import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts.maintain_state import maintain
from scripts.actions_state import stats
from state_store import StateStore
from ccf_paper_scout import metadata, sources


class MaintenanceTests(unittest.TestCase):
    def test_cleanup_copy_preserves_protected_tables_and_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'paper_scout.sqlite3'
            store = StateStore(path)
            paper = dict(id='conf/icml/one', title='World model', year=2025, abstract='')
            store.save_discovery_page('icml', 2025, [paper], {})
            run = store.start_run('test')
            store.record_selection(run, [paper])
            store.finish_delivery(run, True)
            store.save_feedback(paper['id'], {'saved': True})
            with store.connection:
                for key, size, stamp in [('expired', 4, 1), ('new', 6, 999999), ('overflow', 6, 999998)]:
                    store.connection.execute('INSERT INTO source_http_cache VALUES (?,?,?,?,?)', (key, 'pmlr', '{}', size, stamp))
                for model in ('current', 'retired'):
                    store.connection.execute('INSERT INTO paper_embeddings VALUES (?,?,?,?,?,?)', (paper['id'], 'hash', model, 1, b'1234', 'now'))
            tables = [r[0] for r in store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
                      if r[0] not in ('source_http_cache', 'paper_embeddings')]
            protected = {t: store.connection.execute('SELECT * FROM ' + t).fetchall() for t in tables}
            store.close()
            original = path.read_bytes()
            settings = dict(http_bytes=8, max_age_days=1, keep_signatures=['current'], now=1000000)
            preview = maintain(path, **settings)
            self.assertEqual((preview['expired_http_records'], preview['over_budget_http_records'], preview['removed_vectors']), (1, 1, 1))
            output = Path(tmp) / 'cleaned.sqlite3'
            result = maintain(path, output, **settings)
            self.assertFalse(result['dry_run'])
            self.assertEqual(original, path.read_bytes())
            con = sqlite3.connect(output)
            try:
                for table, rows in protected.items():
                    self.assertEqual(con.execute('SELECT * FROM ' + table).fetchall(), rows, table)
                self.assertEqual(con.execute('SELECT cache_key FROM source_http_cache').fetchall(), [('new',)])
                self.assertEqual(con.execute('SELECT model_signature FROM paper_embeddings').fetchall(), [('current',)])
            finally:
                con.close()
            with self.assertRaises(ValueError):
                maintain(path, path)
            with self.assertRaises(ValueError):
                maintain(path, output)
            with self.assertRaises(ValueError):
                maintain(path, keep_signatures=['misspelled'])

    def test_old_database_without_cache_tables_can_be_inspected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'old.sqlite3'
            con = sqlite3.connect(p)
            con.execute('CREATE TABLE papers(id TEXT)')
            con.close()
            self.assertEqual(maintain(p)['removed_vectors'], 0)
            self.assertEqual(maintain(p, Path(tmp)/'copy.sqlite3')['candidates_removed'], 0)

    def test_stats_distinguishes_active_and_all_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = StateStore(root / 'paper_scout.sqlite3')
            store.save_discovery_page('icml', 2025, [dict(id='conf/icml/a', title='World model', year=2025, abstract='')], {})
            store.save_discovery_page('acl', 2024, [dict(id='conf/acl/b', title='Language model', year=2024, abstract='')], {})
            store.close()
            result = stats(root, {'venue_keys': ['icml'], 'years': [2025]})
            self.assertEqual(result['candidates']['total'], 2)
            self.assertEqual(result['candidates']['active'], 1)
            self.assertIn('size_method', result)
            rolling = stats(root, {'venue_keys': ['icml'], 'year_policy': {'mode': 'rolling', 'lookback_years': 1}})
            self.assertEqual(len(rolling['active_years']), 2)


class AbstractPriorityTests(unittest.TestCase):
    def papers(self):
        return [dict(id=f'{venue}-{i}', title='World model planning' if i < 5 else 'Other task',
                     abstract='', venue_key=venue, year=2025) for venue in ('icml', 'acl', 'nips') for i in range(10)]

    def test_priority_balances_venues_and_never_mutates_candidates(self):
        papers = self.papers()
        before = json.dumps(papers, sort_keys=True)
        selected = metadata.prioritize_abstracts(papers, ['world model planning'], 5)
        self.assertEqual(len(selected), 5)
        self.assertEqual(len({p['id'] for p in selected}), 5)
        self.assertEqual({p['venue_key'] for p in selected[:3]}, {'icml', 'acl', 'nips'})
        self.assertEqual(json.dumps(papers, sort_keys=True), before)
        reversed_result = metadata.prioritize_abstracts(list(reversed(papers)), ['world model planning'], 5)
        self.assertEqual([p['id'] for p in selected], [p['id'] for p in reversed_result])

    def test_exploration_uses_pool_outside_ranked_quota(self):
        papers = self.papers()
        result = metadata.prioritize_abstracts(papers, ['world model planning'], 5, 0.2)
        ranked = metadata.prioritize_abstracts(papers, ['world model planning'], 4, 0)
        self.assertEqual(result[:4], ranked)
        self.assertNotIn(result[4]['id'], {p['id'] for p in ranked})
        self.assertEqual(metadata.prioritize_abstracts(papers, ['world model'], 0), [])
        self.assertEqual(len(metadata.prioritize_abstracts(papers, ['world model'], 100)), len(papers))

    def test_main_qualification_limits_daily_lookups_but_not_general_search(self):
        from test_sources import public_paper
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp)/'state.sqlite3')
            try:
                verified = public_paper()
                verified.update(id='conf/icml/verified', abstract='', doi='10.1234/verified')
                unknown = dict(verified, id='conf/icml/unknown', proceedings_id='', publication_evidence=[], doi='10.1234/unknown')
                store.save_discovery_page('icml', 2025, [verified, unknown], {})
                config = {'sources': {'enabled': ['openalex'], 'mode': 'production'}, 'discovery_queries': ['world model']}
                for main_only, expected in [(True, 1), (False, 2)]:
                    with patch.object(sources.subprocess, 'Popen', side_effect=OSError('test: do not start worker')), \
                         patch.object(metadata, 'prioritize_abstracts', wraps=metadata.prioritize_abstracts) as prioritize:
                        sources.discover(store, config, [{'dblp_key': 'icml'}], [2025], 'test', require_main=main_only)
                    self.assertEqual(len(prioritize.call_args.args[0]), expected)
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
