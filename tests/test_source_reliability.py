import copy
import json
import os
from pathlib import Path
import tempfile
import time
import subprocess
import sys
import unittest
from unittest import mock

from ccf_paper_scout import official_abstracts, source_adapters as adapters, source_http, source_worker, sources
from state_store import StateStore
from test_sources import Response


ABSTRACT = 'We learn predictive world models for reinforcement learning and evaluate planning with imagined trajectories in control tasks.'
DIRECTORY = '<html>' + ''.join(f'<a href="/v267/{name}.html">World model {name}</a>' for name in ('a', 'b')) + '</html>'


def detail(name='b', abstract=ABSTRACT):
    return f'<meta name="citation_title" content="World model {name}"><meta name="citation_author" content="Ada"><div id="abstract">{abstract}</div>'


class RecoveryTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows virtualenv launcher behavior')
    def test_cancel_stops_real_windows_worker_not_just_its_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code = "import pathlib,sys,time;p=pathlib.Path(sys.argv[1]);(p/'ready').touch();time.sleep(2);(p/'continued').touch()"
            process = subprocess.Popen([sys.executable, '-c', code, str(root)],
                creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                deadline = time.monotonic()+5
                while not (root/'ready').exists() and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertTrue((root/'ready').exists())
                sources.terminate(process)
                time.sleep(2.1)
                self.assertFalse((root/'continued').exists())
            finally:
                sources.terminate(process)

    def setUp(self):
        self.task = dict(source='pmlr', years=[2025], venue_keys=['icml'], queries=['world model'], states={},
                         settings={'papers_per_volume': 2}, lookups=[])

    def test_single_failure_commits_directory_and_later_paper(self):
        for error in [source_http.SourceError('not_found', 'http_404'),
                      source_http.SourceError('timeout', 'transport_timeout'), '<html>No metadata</html>']:
            with self.subTest(error=error):
                runtime = mock.Mock()
                runtime.get.side_effect = [DIRECTORY, error, detail()]
                pages = [copy.deepcopy(p) for p in adapters.official(self.task, runtime)]
                self.assertEqual(len(pages), 4)
                self.assertEqual(pages[0]['records'], [])
                self.assertEqual(len(pages[0]['state']['entries']), 2)
                self.assertTrue(pages[1]['diagnostics']['failures'])
                self.assertEqual(pages[2]['records'][0]['abstract'], ABSTRACT)

    def test_rate_limit_stops_before_next_paper(self):
        runtime = mock.Mock()
        runtime.get.side_effect = [DIRECTORY, source_http.SourceError('rate_limited', 'http_429', 60), detail()]
        pages = adapters.official(self.task, runtime)
        next(pages)
        failure = next(pages)
        self.assertIn('http_429', failure['diagnostics']['failures'])
        with self.assertRaises(source_http.SourceError):
            next(pages)
        self.assertEqual(runtime.get.call_count, 2)

    def test_resume_after_budget_uses_saved_directory_without_duplicate_candidate(self):
        runtime = mock.Mock()
        runtime.get.side_effect = [DIRECTORY, detail('a'), source_http.SourceError('partial', 'request_budget')]
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp)/'state.sqlite3')
            try:
                with self.assertRaises(source_http.SourceError):
                    for page in adapters.official(self.task, runtime):
                        store.save_source_page('pmlr', page)
                self.assertEqual(len(store.search_candidates()), 1)
                self.task['states'] = store.source_states('pmlr')
                resumed = mock.Mock()
                resumed.get.return_value = detail('b')
                for page in adapters.official(self.task, resumed):
                    store.save_source_page('pmlr', page)
                self.assertEqual(resumed.get.call_count, 1)
                self.assertEqual(len(store.search_candidates()), 2)
                self.task['states'] = store.source_states('pmlr')
                list(adapters.official(self.task, resumed))
                self.assertEqual(resumed.get.call_count, 1)
            finally:
                store.close()

    def test_two_consecutive_network_failures_stop_source(self):
        runtime = mock.Mock()
        runtime.get.side_effect = [DIRECTORY] + [source_http.SourceError('unavailable', 'connection_failed')]*2
        pages = adapters.official(self.task, runtime)
        next(pages); next(pages); next(pages)
        with self.assertRaises(source_http.SourceError):
            next(pages)

    def test_description_and_unrelated_structured_data_are_not_abstracts(self):
        document = detail(abstract='') + f'<meta name="description" content="{ABSTRACT}">'
        document += '<script type="application/ld+json">' + json.dumps([
            {'@type': 'WebSite', 'abstract': ABSTRACT}, {'@type': 'ScholarlyArticle', 'name': 'Other paper', 'abstract': ABSTRACT}]) + '</script>'
        self.assertEqual(official_abstracts.parse_abstract(document, dict(title='World model b', authors=['Ada']), allow_missing=True), '')

    def test_aaai_section_ends_before_download_and_citation_navigation(self):
        document = detail().replace('<div id="abstract">', '<section class="item abstract"><h2>Abstract</h2>').replace('</div>', '</section>')
        document += '<section>Downloads PDF Published How to Cite More Citation Formats Issue Vol.39</section>'
        parsed = official_abstracts.PaperHTML(); parsed.feed(document)
        self.assertEqual(official_abstracts.abstract_fields(parsed)['abstract'], ABSTRACT)
        self.assertFalse(official_abstracts.usable_abstract('Downloads PDF Published How to Cite More Citation Formats Issue Vol.39 ' * 3))

    def test_429_does_not_retry_even_when_retry_after_fits_budget(self):
        runtime = source_http.Runtime({'max_requests': 4, 'deadline_seconds': 10}, {'api.openalex.org'})
        try:
            with mock.patch.object(runtime.session, 'get', return_value=Response(429, headers={'Retry-After': '1'})) as get:
                with self.assertRaises(source_http.SourceError) as failure:
                    runtime.get('https://api.openalex.org/works')
            self.assertEqual(failure.exception.status, 'rate_limited')
            self.assertEqual(get.call_count, 1)
        finally:
            runtime.close()

    def test_lookup_identity_failure_is_deferred_without_stopping_next_lookup(self):
        self.task['lookups'] = [dict(id=name, title=f'World model {name}', authors=['Ada'],
            year=2025, ee=f'https://proceedings.mlr.press/v267/{name}.html') for name in ('a', 'b')]
        runtime = mock.Mock()
        runtime.get.side_effect = [detail('a').replace('content="Ada"', 'content="Other"'), detail('b')]
        pages = list(adapters.lookup(self.task, runtime))
        self.assertEqual(pages[0]['state']['code'], 'official_author_mismatch')
        self.assertEqual(pages[0]['records'], [])
        self.assertEqual(pages[1]['records'][0]['abstract'], ABSTRACT)

    def test_doi_mismatch_cannot_supply_an_abstract(self):
        self.task.update(source='crossref', lookups=[dict(id='a', doi='10.1234/expected')])
        runtime = mock.Mock()
        runtime.json.return_value = {'message': {'DOI': '10.1234/other', 'title': ['Other paper'], 'abstract': ABSTRACT}}
        pages = list(adapters.lookup(self.task, runtime))
        self.assertEqual(pages[0]['records'], [])
        self.assertEqual(pages[0]['state']['code'], 'lookup_identity_mismatch')

    def test_directory_retry_is_visible_without_a_new_request(self):
        key = adapters.key('pmlr', 'https://proceedings.mlr.press/v267/')
        self.task['states'][key] = dict(retry_at='2099-01-01T00:00:00+00:00', code='transport_timeout')
        runtime = mock.Mock()
        pages = list(adapters.official(self.task, runtime))
        runtime.get.assert_not_called()
        self.assertEqual(pages[0]['diagnostics']['deferred_reasons'], {'transport_timeout': 1})

    def test_parser_version_change_revisits_cached_details(self):
        runtime = mock.Mock()
        runtime.get.side_effect = [DIRECTORY, detail('a'), detail('b')]
        pages = list(adapters.official(self.task, runtime))
        self.task['states'][pages[-1]['task_key']] = copy.deepcopy(pages[-1]['state'])
        with mock.patch.object(official_abstracts, 'PARSER_VERSION', 'next-parser'):
            resumed = mock.Mock(); resumed.get.side_effect = [detail('a'), detail('b')]
            pages = list(adapters.official(self.task, resumed))
        self.assertEqual(resumed.get.call_count, 2)  # Directory reused; HTTP cache can supply both bodies.
        self.assertEqual(pages[-1]['records'][0]['abstract_parser_version'], 'next-parser')

    def test_valid_abstract_survives_missing_and_conflicting_official_updates(self):
        from test_sources import public_paper
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp)/'state.sqlite3')
            try:
                original = public_paper()
                store.save_source_page('pmlr', adapters.page('one', [original], {}))
                for text in ('', ABSTRACT):
                    store.save_source_page('pmlr', adapters.page('two', [dict(original, abstract=text)], {}))
                    current = store.search_candidates()[0]
                    self.assertEqual(current['abstract'], original['abstract'])
                    self.assertEqual(current['abstract_status'], 'valid')
                self.assertEqual(current['abstract_diagnostic'], 'conflicting_sources')
            finally:
                store.close()


class CacheTests(unittest.TestCase):
    def runtime(self, **settings):
        runtime = source_http.Runtime(dict(max_requests=4, min_interval_seconds=.001, **settings), {'api.openalex.org'})
        self.addCleanup(runtime.close)
        return runtime

    def test_fresh_cache_avoids_network_and_distinguishes_access_context(self):
        runtime = self.runtime()
        with mock.patch.object(runtime.session, 'get', return_value=Response(body='body')) as get:
            for _ in range(2):
                self.assertEqual(runtime.get('https://api.openalex.org/works', params={'search': 'RL'}, headers={'Authorization':'secret'}), 'body')
            runtime.get('https://api.openalex.org/works', params={'search': 'planning'}, headers={'Authorization':'secret'})
            runtime.get('https://api.openalex.org/works', params={'search': 'RL'}, headers={'Authorization':'other'})
        self.assertEqual(get.call_count, 3)
        self.assertEqual(runtime.cache_hits, 1)
        self.assertNotIn('secret', json.dumps(runtime.cache))

    def test_expired_etag_response_uses_304_body_and_preserves_validator(self):
        runtime = self.runtime()
        with mock.patch.object(runtime.session, 'get', side_effect=[Response(body='body', headers={'ETag':'v1'}), Response(304)]) as get:
            runtime.get('https://api.openalex.org/works', cache_seconds=0)
            self.assertEqual(runtime.get('https://api.openalex.org/works', cache_seconds=0), 'body')
        self.assertEqual(get.call_args.kwargs['headers']['If-None-Match'], 'v1')
        self.assertEqual(runtime.requests, 2)
        self.assertEqual(runtime.cache_hits, 1)
        self.assertEqual(next(iter(runtime.cache.values()))['etag'], 'v1')

    def test_304_without_cache_retries_within_budget(self):
        runtime = self.runtime()
        with mock.patch.object(runtime.session, 'get', side_effect=[Response(304), Response(body='body')]):
            self.assertEqual(runtime.get('https://api.openalex.org/works'), 'body')
        self.assertEqual(runtime.requests, 2)
        runtime = self.runtime()
        runtime.settings['max_requests'] = 1
        with mock.patch.object(runtime.session, 'get', return_value=Response(304)):
            with self.assertRaises(source_http.SourceError) as error:
                runtime.get('https://api.openalex.org/works')
        self.assertEqual(error.exception.code, 'request_budget')

    def test_persistent_cache_survives_restart_and_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp)/'state.sqlite3')
            try:
                for n in range(70):
                    store.save_http_cache('openalex', dict(cache_key=str(n), body='body', fetched_at=time.time()+n))
                self.assertEqual(len(store.http_cache('openalex')), 64)
                store.close()
                store = StateStore(Path(tmp)/'state.sqlite3')
                self.assertEqual(len(store.http_cache('openalex')), 64)
                self.assertNotIn('0', store.http_cache('openalex'))
            finally:
                store.close()

    def test_hard_deadline_cannot_bypass_saved_pages_and_cache(self):
        task = dict(source='openalex', settings={}, states={}, queries=['RL'], years=[2025])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def collect(_task, runtime):
                runtime.cache_response('key', 'body', {})
                yield adapters.page('task', [], {'pending': True})
                raise source_http.SourceError('partial', 'source_deadline')
            with mock.patch.object(source_worker, 'collect', side_effect=collect):
                source_worker.run(task, root)
            self.assertEqual(len(list(root.glob('cache-*.json'))), 1)
            self.assertTrue((root/'page-00000.json').exists())
            self.assertEqual(json.loads((root/'result.json').read_text())['status'], 'partial')
