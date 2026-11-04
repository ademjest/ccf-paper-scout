import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import paper_scout as scout
from ccf_paper_scout import discovery
from state_store import StateStore


def paper(name, **changes):
    return {"id": "conf/icml/" + name, "title": "World model planning " + name,
            "abstract": "A world model learns dynamics for planning and reinforcement learning.",
            "authors": ["Test Author"], "year": 2025, "venue": "ICML", "rank": "A", "type": "conference",
            "url": "https://dblp.org/rec/conf/icml/" + name,
            "proceedings_id": "https://dblp.org/rec/conf/icml/2025", **changes}


class CachedDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = StateStore(self.root / "state.sqlite3")
        self.config = {"years": [2025], "venue_keys": ["icml"], "state_db": str(self.store.path),
                       "seen_db": str(self.root / "seen.json"), "max_results": 5,
                       "explicit_interests": ["world model"], "topic_priority": {"primary_topics": ["world model"]},
                       "dblp": {"allow_cached_on_transient_failure": True, "page_size": 1,
                                "max_pages_per_run": 2, "max_pages_per_partition_per_run": 2},
                       "metadata": {"max_requests_per_run": 0}, "delivery": {"smtp": {"enabled": True}}}
        (self.root / "interests.json").write_text('[{"title": "Planning reference"}]', encoding="utf-8")
        (self.root / "venues.json").write_text(json.dumps({"venues": [{"dblp_key": "icml", "abbr": "ICML",
            "name": "ICML", "rank": "A", "type": "conference"}]}), encoding="utf-8")
        self.args = argparse.Namespace(config=self.root / "config.json", interests=self.root / "interests.json",
            venues=self.root / "venues.json", output=self.root / "report.md", labels=None,
            no_update_seen=False, discover_only=False, evaluate_only=False)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def save(self, *papers):
        self.store.save_discovery_page("icml", 2025, list(papers), {})

    def run_failure(self, error=None, side_effect=None):
        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch.object(scout, "fetch_dblp_page", side_effect=side_effect or error or scout.DblpRequestError("HTTP 503")), \
             mock.patch.object(scout, "lookup_dblp_proceedings") as backfill, \
             mock.patch.object(scout, "send_email", return_value=True) as send:
            self.assertEqual(scout.run_pipeline(self.args, self.config), 0)
        backfill.assert_not_called()
        self.sent = send
        self.diagnostics = json.loads(self.args.output.with_suffix('.diagnostics.json').read_text(encoding='utf-8'))

    def test_transient_failure_uses_prescan_cache_and_delivers_notice(self):
        cached, fresh = paper("Cached"), paper("Fresh")
        self.save(cached)
        self.run_failure(side_effect=[([fresh], None, 1), scout.DblpRequestError("HTTP 503")])
        self.assertEqual(self.diagnostics['selected_ids'], [cached['id']])
        self.assertEqual(self.diagnostics['discovery']['mode'], 'cached_snapshot')
        self.assertEqual(self.diagnostics['discovery']['success_ratio'], 0)
        self.assertTrue(self.diagnostics['source_failures'][0]['transient'])
        self.sent.assert_called_once()
        report = self.sent.call_args.args[1]
        self.assertIn('本次未完成实时检索', report)
        self.assertIn('本次扫描开始前已有的缓存候选', report)
        self.assertNotIn('推荐结果不包含这些来源', report)
        self.assertEqual(self.store.delivered_ids(), {cached['id']})
        self.assertEqual(self.store.load_cursor('DBLP', 'icml', 2025), 1)
        self.assertFalse((self.root / '.delivery-pending').exists())

    def test_cached_candidates_still_obey_quality_and_dedup_gates(self):
        accepted = paper('Accepted')
        zotero = paper('Library', title='A saved library title')
        self.save(accepted, paper('Sent'), zotero, paper('MissingAbstract', abstract=''),
                  paper('UnknownTrack', proceedings_id=''),
                  paper('Unrelated', title='Ceramic classification', abstract='Ancient pottery.'),
                  paper('WrongRank', rank='B'))
        (self.root / 'seen.json').write_text(json.dumps({'ids': ['conf/icml/Sent']}), encoding='utf-8')
        self.args.interests.write_text(json.dumps([zotero]), encoding='utf-8')
        self.run_failure()
        self.assertEqual(self.diagnostics['selected_ids'], [accepted['id']])
        self.assertIn('conf/icml/Sent', self.store.delivered_ids())

    def test_empty_cache_does_not_send_or_accept_partial_new_scan(self):
        self.run_failure(side_effect=[([paper('Fresh')], None, 1), scout.DblpRequestError('timeout')])
        self.sent.assert_not_called()
        self.assertEqual(self.diagnostics['selected_ids'], [])
        self.assertEqual(self.store.delivered_ids(), set())

    def test_cache_without_qualified_papers_does_not_send(self):
        self.save(paper('Unknown', proceedings_id=''))
        self.run_failure()
        self.sent.assert_not_called()
        self.assertEqual(self.diagnostics['selected_ids'], [])

    def test_protocol_errors_are_not_treated_as_transient(self):
        self.save(paper('Cached'))
        with self.assertRaises(discovery.DiscoveryUnavailableError):
            self.run_failure(error=scout.DblpProtocolError('invalid JSON'))

    def test_cli_preview_fallback_leaves_original_state_unchanged(self):
        cached = paper('Cached')
        self.save(cached)
        self.args.config.write_text(json.dumps(self.config), encoding='utf-8')
        before = self.store.scan_state('icml', 2025)
        argv = ['paper_scout', '--config', str(self.args.config), '--interests', str(self.args.interests),
                '--venues', str(self.args.venues), '--output', str(self.args.output), '--no-update-seen']
        with mock.patch('sys.argv', argv), \
             mock.patch.object(scout, 'fetch_dblp_page', side_effect=scout.DblpRequestError('timeout')), \
             mock.patch.object(scout, 'send_email') as send:
            self.assertEqual(scout.main(), 0)
        send.assert_not_called()
        self.assertEqual(self.store.scan_state('icml', 2025), before)
        self.assertEqual(self.store.delivered_ids(), set())
        self.assertFalse((self.root / 'seen.json').exists())
        self.assertIn('本次未完成实时检索', self.args.output.read_text(encoding='utf-8'))

    def test_fallback_can_be_disabled_and_never_masks_discover_only_failure(self):
        self.save(paper('Cached'))
        for enabled, strict, discover_only in [(False, False, False), (True, True, False), (True, False, True)]:
            with self.subTest(enabled=enabled, strict=strict, discover_only=discover_only):
                self.config['dblp'].update(allow_cached_on_transient_failure=enabled,
                                           failure_policy='strict' if strict else 'continue')
                self.args.discover_only = discover_only
                with self.assertRaises(discovery.DiscoveryUnavailableError):
                    self.run_failure()


if __name__ == '__main__':
    unittest.main()
