import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import requests

import paper_scout
from state_store import StateStore
from ccf_paper_scout import search, sources, source_adapters, source_http
from ccf_paper_scout.web import SearchServer
from test_sources import public_paper


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = dict(venue_keys=['icml'], years=[2025], state_db='daily.sqlite3',
            sources=dict(enabled=['pmlr'], mode='production'), explicit_interests=['world model'])
        self.path = self.root/'search.sqlite3'
        store = StateStore(self.path)
        store.save_source_page('pmlr', dict(task_key='pmlr', records=[public_paper()], state={}))
        preprint = dict(source_record_id='2501.01234', title='World model for navigation',
                        year=2025, publication_status='preprint', url='https://arxiv.org/abs/2501.01234')
        store.save_source_page('arxiv', dict(task_key='arxiv', records=[preprint], state={}))
        self.ids = {p['id'] for p in store.search_candidates()}
        store.close()

    def tearDown(self):
        self.temp.cleanup()

    def test_scopes_keep_preprints_searchable_without_admitting_them_as_main(self):
        with mock.patch.object(sources, 'discover') as discover, mock.patch.object(paper_scout, 'send_email') as mail:
            all_results = search.run(self.config, self.path, 'world model', cached_only=True)
            main = search.run(self.config, self.path, 'world model', scope='verified_main', cached_only=True)
        self.assertEqual(len(all_results['results']), 2)
        self.assertEqual(len(main['results']), 1)
        self.assertEqual(main['results'][0]['publication_label'], 'verified_main')
        discover.assert_not_called(); mail.assert_not_called()

    def test_cached_first_and_cancel_does_not_change_delivery_history(self):
        event = threading.Event()
        def cached(result):
            self.assertEqual(result['state'], 'cached')
            self.assertEqual(len(result['results']), 2)
            event.set()
        with mock.patch.object(sources, 'discover') as discover:
            result = search.run(self.config, self.path, 'world model', cancel=event, update=cached)
        discover.assert_not_called()
        self.assertEqual(result['state'], 'cancelled')
        store = StateStore(self.path)
        self.assertEqual(store.delivered_ids(), set())
        store.close()

    def test_abstract_filter_is_explicit_and_part_of_frozen_request(self):
        original = search.run(self.config, self.path, 'world model', cached_only=True)
        filtered = search.run(self.config, self.path, 'world model', cached_only=True, require_abstract=True)
        self.assertEqual(len(original['results']), 2)
        self.assertEqual(len(filtered['results']), 1)
        self.assertTrue(filtered['request']['require_abstract'])
        self.assertEqual(filtered['counts']['abstract_filtered'], 1)
        self.assertEqual(filtered['counts']['displayed_with_abstract'], 1)
        from ccf_paper_scout import search_history
        store = StateStore(self.path)
        restored = search_history.get(store, original['id'])
        store.close()
        self.assertEqual(len(restored['results']), 2)
        self.assertFalse(restored['request']['require_abstract'])

    def test_daily_snapshot_is_separate_including_when_daily_uses_wal(self):
        daily = StateStore(self.root/'daily.sqlite3')
        daily.save_source_page('pmlr', dict(task_key='seed', records=[public_paper()], state={}))
        path = search.database(self.config, self.root/'config.json')
        manual = StateStore(path)
        self.assertEqual(len(manual.search_candidates()), 1)
        manual.save_feedback(manual.search_candidates()[0]['id'], {'read':True})
        self.assertEqual(daily.connection.execute('SELECT COUNT(*) FROM paper_feedback').fetchone()[0], 0)
        manual.close(); daily.close()
        with self.assertRaises(ValueError):
            search.database({**self.config, 'search': {'state_db':'daily.sqlite3'}}, self.root/'config.json')

    def test_all_sources_failed_retains_cache_and_reports_failure(self):
        failure = dict(mode='cached_only', providers={'openalex': {'status':'timeout', 'stored':0}})
        with mock.patch.object(sources, 'discover', return_value=failure), mock.patch.object(paper_scout, 'fetch_zotero') as zotero:
            result = search.run(self.config, self.path, 'world model')
        self.assertEqual(len(result['results']), 2)
        self.assertEqual(result['discovery']['mode'], 'cached_only')
        zotero.assert_not_called()

    def test_shadow_does_not_update_production_candidates_or_cursors(self):
        store = StateStore(self.path)
        config = {**self.config, 'sources': {'enabled':['pmlr'], 'mode':'shadow'}}
        def pages(target, source, state):
            target.save_source_page(source, dict(task_key='new', records=[dict(public_paper(),
                title='Novel agent', source_record_id='new', url='https://example.org/new', ee='')], state={'offset':99}))
        process = mock.Mock(returncode=1)
        process.poll.return_value = 1
        with mock.patch.object(sources.subprocess, 'Popen', return_value=process), mock.patch.object(sources, 'consume_pages', side_effect=pages):
            result = sources.discover(store, config, [], [2025], 'shadow')
        self.assertTrue(result['shadow'])
        self.assertNotIn('new', store.source_states('pmlr'))
        self.assertEqual({p['id'] for p in store.search_candidates()}, self.ids)
        store.close()

    def test_budget_rejects_fractional_concurrency_and_attempt_counts(self):
        for changes in ({'max_parallel_sources':.5}, {'providers':{'pmlr':{'max_attempts':1.5}}},
                        {'providers':{'pmlr':{'page_size':0}}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                sources.validate(dict(enabled=['pmlr'], **changes))

    def test_cancelled_worker_does_not_trigger_source_cooldown(self):
        event = threading.Event()
        store = StateStore(self.path)
        process = mock.Mock(returncode=-15)
        process.poll.side_effect = lambda: None if not event.is_set() else -15
        def launch(*args, **kwargs):
            event.set()
            return process
        with mock.patch.object(sources.subprocess, 'Popen', side_effect=launch):
            result = sources.discover(store, self.config, [], [2025], 'cancel-test', cancel=event)
        self.assertEqual(result['providers']['pmlr']['status'], 'cancelled')
        self.assertNotIn('retry_at', store.source_health('pmlr'))
        store.close()

    def test_exact_doi_lookup_checks_response_identity_and_caches_absence(self):
        task = dict(source='crossref', states={}, lookups=[dict(id='old',doi='10.1234/a')])
        runtime = mock.Mock()
        runtime.json.return_value = {'message': {'DOI':'10.1234/b','title':['Different work']}}
        with self.assertRaises(source_http.SourceError):
            list(source_adapters.lookup(task, runtime))
        runtime.json.side_effect = source_http.SourceError('not_found', 'http_404')
        result = list(source_adapters.lookup(task, runtime))
        self.assertEqual(result[0]['records'], [])
        self.assertIn('retry_at', result[0]['state'])

    def test_multisource_preview_leaves_original_database_unchanged(self):
        original = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.config.update(state_db=str(self.path), seen_db=str(self.root/'seen.json'), max_results=5,
            discovery_queries=['world model'], delivery={'smtp': {'enabled':True}},
            semantic={'enabled':False}, llm_translation={'enabled':False}, topic_priority={'primary_topics':['world model']})
        config_path = self.root/'config.json'
        config_path.write_text(json.dumps(self.config), encoding='utf-8')
        interests = self.root/'interests.json'; interests.write_text('[]', encoding='utf-8')
        output = self.root/'report.md'
        def fake(store, *_args, **_kwargs):
            store.save_source_page('pmlr', dict(task_key='changed', records=[public_paper()], state={'done':['new']}))
            return {'mode':'partial_live','providers':{'pmlr':{'status':'ok','stored':1}, 'dblp':{'status':'timeout'}}}
        with mock.patch.object(sources, 'discover', side_effect=fake), mock.patch.object(paper_scout, 'send_email') as mail, mock.patch.object(paper_scout.metadata, 'enrich') as metadata:
            from ccf_paper_scout.cli import main
            self.assertEqual(main(['run','--config',str(config_path),'--interests',str(interests),'--output',str(output),'--no-update-seen']), 0)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), original)
        self.assertTrue(output.exists())
        mail.assert_not_called(); metadata.assert_not_called()


class WebTests(unittest.TestCase):
    def test_http_search_feedback_cancel_and_local_origin(self):
        with tempfile.TemporaryDirectory() as temp:
            config = dict(venue_keys=['icml'], years=[2025])
            server = SearchServer(config, Path(temp)/'config.json', 0)
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval':.05})
            thread.start()
            url = 'http://127.0.0.1:' + str(server.server_port)
            headers = {'X-Scout-Token': server.token}
            store = StateStore(server.database)
            identifier = store.save_source_page('pmlr', dict(task_key='x', records=[public_paper()], state={}))[0]
            store.close()
            def discovery(*_args, cancel, progress):
                cancel.wait(timeout=3)
                return {'mode':'cached_only','providers':{}}
            try:
                self.assertEqual(requests.get(url,timeout=3).status_code, 200)
                self.assertEqual(requests.get(url,headers={'Host':'attacker.invalid'},timeout=3).status_code, 403)
                self.assertEqual(requests.post(url+'/searches',json={'query':'world model'},timeout=3).status_code, 403)
                with mock.patch.object(sources,'discover',side_effect=discovery):
                    response = requests.post(url+'/searches',headers=headers,json={'query':'world model'},timeout=3)
                    self.assertEqual(response.status_code, 202)
                    job = response.json()['id']
                    self.assertEqual(requests.post(url+'/searches',headers=headers,json={'query':'planning'},timeout=3).status_code,409)
                    self.assertEqual(requests.delete(url+'/searches/'+job,headers=headers,timeout=3).status_code,202)
                    server.worker.join(timeout=4)
                    self.assertFalse(server.worker.is_alive())
                    result = requests.get(url+'/searches/'+job,timeout=3).json()
                    self.assertEqual(result['state'],'cancelled')
                response=requests.post(url+'/papers/'+identifier+'/feedback',headers=headers,json={'read':True},timeout=3)
                self.assertEqual(response.json(), {'read':True})
                store=StateStore(server.database)
                self.assertEqual(store.delivered_ids(),set());store.close()
            finally:
                server.shutdown();thread.join(timeout=3);server.server_close()


if __name__ == '__main__':
    unittest.main()
