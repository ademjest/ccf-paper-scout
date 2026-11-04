import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import requests

from state_store import StateStore
from ccf_paper_scout import search, search_history as history, search_evaluation as evaluation
from ccf_paper_scout.web import SearchServer


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root/'search.sqlite3'
        self.config = dict(years=[2025], venue_keys=['icml'])
        self.store = StateStore(self.path)
        papers = [dict(source_record_id=str(i), title=title, year=2025,
            abstract='We study world models for planning and reinforcement learning, using learned dynamics to improve sequential decisions.',
            url='https://example.org/'+str(i)) for i, title in enumerate(
                ['World model planning', 'World model reinforcement learning', 'World model reasoning'])]
        self.store.save_source_page('openalex', dict(task_key='seed', records=papers, state={}))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def run_search(self, query='world model', split='dev'):
        return search.run(self.config, self.path, query, split=split, scope='all_with_preprints', cached_only=True)

    def label(self, result, index, grade, **kwargs):
        with self.store.connection:
            return history.label(self.store, result['id'], result['results'][index]['id'], grade, **kwargs)

    def test_snapshot_survives_cache_changes_and_restart(self):
        result = self.run_search()
        original = history.get(self.store, result['id'])
        paper = self.store.search_candidates()[0]
        paper['title'] = 'Changed after search'
        self.store.update_candidates([paper])
        self.store.close(); self.store = StateStore(self.path)
        restored = history.get(self.store, result['id'])
        self.assertEqual(restored, original)
        self.assertEqual(history.recent(self.store)[0]['query'], 'world model')
        self.assertIn('cache_seconds', restored['timing'])
        self.assertEqual(restored['counts']['matched'], 3)
        with self.assertRaises(ValueError):
            history.save(self.store, result)

    def test_same_paper_can_receive_different_query_judgments(self):
        a = self.run_search('world model')
        b = self.run_search('world planning')
        paper_id = a['results'][0]['id']
        with self.store.connection:
            history.label(self.store, a['id'], paper_id, 2)
            history.label(self.store, b['id'], paper_id, 0)
        values = [next(p for p in history.get(self.store, r['id'])['results'] if p['id'] == paper_id)
                  ['judgments']['human']['grade'] for r in (a, b)]
        self.assertEqual(values, [2, 0])
        self.assertEqual(self.store.delivered_ids(), set())

    def test_legacy_global_relevance_is_not_reinterpreted_as_query_label(self):
        paper_id = self.store.search_candidates()[0]['id']
        self.store.save_feedback(paper_id, dict(relevance='relevant', saved=True))
        result = self.run_search()
        paper = next(p for p in result['results'] if p['id'] == paper_id)
        self.assertEqual(paper['feedback'], {'saved':True})
        self.assertEqual(paper['judgments'], {})
        raw = self.store.connection.execute('SELECT payload_json FROM paper_feedback WHERE paper_id=?', (paper_id,)).fetchone()[0]
        self.assertEqual(json.loads(raw)['relevance'], 'relevant')

    def test_same_normalized_query_cannot_cross_dev_and_test(self):
        self.run_search('world model')
        with self.assertRaises(ValueError):
            self.run_search(' World   MODEL ', 'test')

    def test_ai_cannot_overwrite_human_and_holdout_never_enters_packet(self):
        dev = self.run_search()
        test = self.run_search('planning reinforcement', 'test')
        self.label(dev, 0, 0, reason='Not about the requested planning method')
        self.label(test, 0, 2)
        packet = evaluation.ai_packet(evaluation.export(self.store))
        self.assertEqual(len(packet['examples']), 1)
        self.assertTrue(all(t['search_id'] == dev['id'] for t in packet['labels']))
        self.label(dev, 0, 2, origin='ai', actor='test-model', reason='Disagree', snapshot_hash=dev['snapshot_hash'])
        result = history.get(self.store, dev['id'])
        labels = result['results'][0]['judgments']
        self.assertEqual((labels['human']['grade'], labels['ai']['grade']), (0, 2))
        with self.assertRaises(ValueError):
            self.label(test, 0, 1, origin='ai', actor='test-model', reason='Not allowed', snapshot_hash=test['snapshot_hash'])

    def test_ai_import_is_atomic_and_requires_matching_snapshot(self):
        result = self.run_search()
        packet = evaluation.ai_packet(evaluation.export(self.store))
        for row in packet['labels']:
            row.update(grade=1, reason='Partly relevant', actor='test-model')
        packet['labels'][-1]['snapshot_hash'] = 'bad'
        with self.assertRaises(ValueError):
            evaluation.import_ai(self.store, packet)
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM query_feedback').fetchone()[0], 0)
        packet['labels'][-1]['snapshot_hash'] = result['snapshot_hash']
        self.assertEqual(evaluation.import_ai(self.store, packet), 3)

    def test_ai_packet_defers_unlabeled_papers_without_abstracts(self):
        paper = self.store.search_candidates()[0]
        paper['abstract'] = ''
        self.store.update_candidates([paper])
        self.run_search()
        packet = evaluation.ai_packet(evaluation.export(self.store))
        self.assertEqual(packet['deferred_missing_abstract'], 1)
        self.assertEqual(len(packet['labels']), 2)

    def test_label_requires_result_membership_and_valid_grade(self):
        result = self.run_search()
        for grade in (True, 3, '2', -1):
            with self.subTest(grade=grade), self.assertRaises(ValueError):
                self.label(result, 0, grade)
        with self.assertRaises(ValueError):
            history.label(self.store, result['id'], 'missing-paper', 2)

    def test_failure_keeps_cached_snapshot_and_error_type(self):
        with mock.patch.object(search.sources, 'discover', side_effect=RuntimeError('private-url')):
            with self.assertRaises(RuntimeError):
                search.run(self.config, self.path, 'world model', scope='all_with_preprints')
        result = history.get(self.store, history.recent(self.store)[0]['id'])
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(len(result['results']), 3)
        self.assertEqual(result['error'], 'RuntimeError')

    def test_missing_labels_are_not_zero_and_ai_is_not_gold(self):
        result = self.run_search('world model', 'test')
        self.label(result, 0, 2)
        data = evaluation.export(self.store)
        report = evaluation.evaluate(data, k=2)
        self.assertIsNone(report['summary']['test']['precision_at_k'])
        self.assertEqual(report['queries'][0]['top_judged'], 1)
        self.label(result, 1, 0)
        report = evaluation.evaluate(evaluation.export(self.store), k=2)
        self.assertEqual(report['summary']['test']['precision_at_k'], .5)
        self.assertIsNone(report['summary']['test']['ndcg_at_k'])

    def test_ai_only_development_labels_never_produce_human_metrics(self):
        result = self.run_search()
        for i in range(3):
            self.label(result, i, 2, origin='ai', actor='test-model', reason='Suggested relevance', snapshot_hash=result['snapshot_hash'])
        report = evaluation.evaluate(evaluation.export(self.store), k=2)
        self.assertEqual(report['queries'][0]['ai_suggestions'], 3)
        self.assertEqual(report['queries'][0]['judged_human'], 0)
        self.assertIsNone(report['summary']['dev']['precision_at_k'])

    def test_metrics_reproduce_and_comparison_requires_same_pool(self):
        result = self.run_search()
        for index, grade in enumerate([2, 1, 0]):
            self.label(result, index, grade)
        data = evaluation.export(self.store)
        a = evaluation.evaluate(data, k=2)
        self.assertEqual(a, evaluation.evaluate(json.loads(json.dumps(data)), k=2))
        self.assertEqual(a['summary']['dev']['precision_at_k'], 1)
        self.assertEqual(a['summary']['dev']['strong_precision_at_k'], .5)
        self.assertEqual(a['summary']['dev']['ndcg_at_k'], 1)
        self.assertEqual(a['summary'], evaluation.evaluate(data, k=2, ranking='lexical')['summary'])
        ranking = dict(schema='scout-search-rankings-v1', queries={result['id']: dict(
            snapshot_hash=result['snapshot_hash'], ranking=list(reversed(data['queries'][0]['ranking'])))})
        self.assertLess(evaluation.evaluate(data, k=2, overrides=ranking)['summary']['dev']['ndcg_at_k'], 1)
        ranking['queries'][result['id']]['ranking'].append('unknown')
        with self.assertRaises(ValueError):
            evaluation.evaluate(data, overrides=ranking)

    def test_short_list_denominator_and_snapshot_tampering(self):
        result = self.run_search()
        for i in range(3):
            self.label(result, i, 2)
        data = evaluation.export(self.store)
        self.assertEqual(evaluation.evaluate(data, k=5)['summary']['dev']['precision_at_k'], .6)
        tampered = copy.deepcopy(data)
        tampered['queries'][0]['papers'][0]['abstract'] = 'Changed evidence'
        with self.assertRaises(ValueError):
            evaluation.evaluate(tampered)
        tampered = copy.deepcopy(data)
        tampered['queries'][0]['split'] = 'test'
        with self.assertRaises(ValueError):
            evaluation.evaluate(tampered)

    def test_latest_snapshot_per_query_and_no_labels_is_not_perfect_score(self):
        old = self.run_search()
        latest = self.run_search()
        data = evaluation.export(self.store)
        self.assertEqual([q['search_id'] for q in data['queries']], [latest['id']])
        report = evaluation.evaluate(data)
        self.assertIsNone(report['summary']['dev']['precision_at_k'])
        self.assertEqual(report['summary']['dev']['precision_at_k_queries'], 0)
        self.assertEqual(evaluation.export(self.store, [old['id']])['queries'][0]['search_id'], old['id'])

    def test_http_history_judgment_and_export_survive_new_server(self):
        result = self.run_search()
        config = {**self.config, 'search':{'state_db':str(self.path)}}
        server = SearchServer(config, self.root/'config.json', 0)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval':.05})
        thread.start()
        url = f'http://127.0.0.1:{server.server_port}'
        try:
            self.assertEqual(requests.get(url+'/searches',timeout=3).json()[0]['id'], result['id'])
            endpoint=url+'/searches/'+result['id']+'/feedback'
            body=dict(paper_id=result['results'][0]['id'],grade=2,reason='Useful for planning')
            self.assertEqual(requests.post(endpoint,json=body,timeout=3).status_code,403)
            self.assertEqual(requests.post(endpoint,headers={'X-Scout-Token':server.token},json=body,timeout=3).status_code,200)
            restored=requests.get(url+'/searches/'+result['id'],timeout=3).json()
            self.assertEqual(restored['results'][0]['judgments']['human']['grade'],2)
            packet=requests.get(url+'/annotation-tasks',timeout=3).json()
            self.assertEqual(len(packet['examples']),1)
            self.assertEqual(len(requests.get(url+'/benchmark',timeout=3).json()['queries']),1)
        finally:
            server.shutdown();thread.join(timeout=3);server.server_close()


if __name__ == '__main__':
    unittest.main()
