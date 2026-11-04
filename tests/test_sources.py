import copy
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import requests
from state_store import StateStore
from ccf_paper_scout import identity, proceedings, source_adapters, source_http, sources


def public_paper(**changes):
    volume = source_adapters.catalog()['volumes'][0]
    url = 'https://proceedings.mlr.press/v267/test25.html'
    return dict(source='pmlr', source_record_id=url, title='World model planning', authors=['Ada Author'],
                year=2025, venue_key='icml', venue='ICML', rank='A', type='conference', url=url, ee=url,
                abstract='A world model learns dynamics for planning with reinforcement learning in a control environment.',
                proceedings_id=volume['proceedings_id'],
                publication_evidence=[dict(kind='official_volume_membership', source='pmlr', paper_url=url,
                    volume_url=volume['url'], venue_key='icml', year=2025, track=volume['track'], entry_track='',
                    content_hash='a'*64, rule_version=source_adapters.catalog()['version'])], **changes)


class Response:
    def __init__(self, code=200, body='{}', headers=None):
        self.status_code, self.body, self.headers = code, body, headers or {}
        self.is_redirect = code in (301, 302, 307, 308)
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def iter_content(self, _): yield self.body.encode()


class HttpTests(unittest.TestCase):
    def runtime(self):
        result = source_http.Runtime(dict(max_requests=3, max_attempts=2, min_interval_seconds=.001, deadline_seconds=5), {'api.openalex.org'})
        self.addCleanup(result.close)
        return result

    def test_retry_after_exceeding_budget_stops_without_early_retry(self):
        runtime = self.runtime()
        with mock.patch.object(runtime.session, 'get', return_value=Response(429, headers={'Retry-After':'100'})) as get:
            with self.assertRaises(source_http.SourceError) as failure:
                runtime.get('https://api.openalex.org/works')
        self.assertEqual((failure.exception.status, failure.exception.retry_after), ('rate_limited', 100))
        self.assertEqual(get.call_count, 1)

    def test_transport_errors_are_bounded_and_do_not_expose_credentials(self):
        runtime = self.runtime()
        with mock.patch.object(runtime, 'wait'), mock.patch.object(runtime.session, 'get', side_effect=requests.Timeout('secret-in-url')) as get:
            with self.assertRaises(source_http.SourceError) as failure:
                runtime.get('https://api.openalex.org/works', headers={'Authorization':'secret'})
        self.assertEqual(get.call_count, 2)
        self.assertNotIn('secret', str(failure.exception))

    def test_invalid_json_and_size_limit_do_not_become_empty_results(self):
        for body, limit in [('<html>maintenance</html>', 100), ('1234567890', 3)]:
            runtime = self.runtime()
            with self.subTest(body=body), mock.patch.object(runtime.session, 'get', return_value=Response(body=body)):
                with self.assertRaises(source_http.SourceError):
                    runtime.json('https://api.openalex.org/works', max_bytes=limit)

    def test_untrusted_redirect_does_not_receive_credentials(self):
        runtime = self.runtime()
        with mock.patch.object(runtime.session, 'get', return_value=Response(302, headers={'Location':'https://attacker.invalid/'})) as get:
            with self.assertRaises(source_http.SourceError):
                runtime.get('https://api.openalex.org/works', headers={'Authorization':'secret'})
        self.assertEqual(get.call_count, 1)

    def test_auth_and_challenge_do_not_retry(self):
        for response in [Response(403), Response(200, "<title>Making sure you're not a bot!</title>")]:
            runtime = self.runtime()
            with mock.patch.object(runtime.session, 'get', return_value=response) as get:
                with self.assertRaises(source_http.SourceError) as failure:
                    runtime.get('https://api.openalex.org/works')
            self.assertEqual(failure.exception.status, 'blocked')
            self.assertEqual(get.call_count, 1)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'state.sqlite3'
        self.store = StateStore(self.path)
    def tearDown(self):
        self.store.close(); self.temp.cleanup()
    def save(self, paper, source='pmlr'):
        return self.store.save_source_page(source, dict(task_key='page', records=[paper], state={'offset':1}))

    def test_official_evidence_admits_paper_without_dblp_id(self):
        identifier = self.save(public_paper())[0]
        self.assertTrue(identifier.startswith('paper:'))
        paper = self.store.candidates(['icml'], [2025])[0]
        self.assertEqual(proceedings.qualify(paper, proceedings.registry()), 'verified')
        for field, value in [('paper_url','https://attacker.invalid/test'), ('entry_track','workshop'), ('year',2026)]:
            altered = copy.deepcopy(paper)
            altered['publication_evidence'][0][field] = value
            self.assertEqual(proceedings.qualify(altered, proceedings.registry()), 'unresolved')

    def test_source_alias_preserves_historical_delivery_identity(self):
        old = public_paper()
        old['id'] = 'conf/icml/Existing25'
        self.store.save_discovery_page('icml', 2025, [old], {})
        run = self.store.start_run('test')
        self.store.record_selection(run, [old])
        self.store.finish_delivery(run, True)
        self.assertEqual(self.save(public_paper()), [old['id']])
        self.assertEqual(self.store.delivered_ids(), {old['id']})
        self.assertEqual(len(self.store.candidates(['icml'], [2025])), 1)
        self.store.close(); self.store = StateStore(self.path)
        self.assertEqual(self.store.delivered_ids(), {old['id']})

    def test_weak_title_collision_is_not_merged_or_resent(self):
        self.save(public_paper())
        incoming = dict(source_record_id='W123', title='World model planning', authors=['Different Author'], year=2025,
                        doi='10.1234/different', url='https://doi.org/10.1234/different')
        self.assertEqual(self.save(incoming, 'openalex'), [])
        payload = self.store.connection.execute("SELECT payload_json FROM source_records WHERE source='openalex'").fetchone()[0]
        self.assertTrue(json.loads(payload)['identity_review_pending'])

    def test_short_subject_metadata_is_not_an_abstract(self):
        paper = dict(public_paper(), abstract='Computer Systems, Imagery and Media')
        self.save(paper)
        self.assertEqual(self.store.search_candidates()[0]['abstract'], '')

    def test_page_transaction_cannot_advance_after_invalid_record(self):
        with self.assertRaises(KeyError):
            self.store.save_source_page('pmlr', dict(task_key='broken', records=[public_paper(), {}], state={'offset':20}))
        self.assertEqual(self.store.source_states('pmlr'), {})
        self.assertEqual(self.store.search_candidates(), [])

    def test_openalex_merge_cannot_erase_official_qualification_or_abstract(self):
        self.save(public_paper())
        incoming = dict(public_paper(), source_record_id='W123', publication_evidence=[], venue_key='', year=2026,
                        abstract='Untrusted replacement', venue='unknown', rank='', proceedings_id='')
        self.save(incoming, 'openalex')
        value = self.store.candidates(['icml'], [2025])[0]
        self.assertEqual(value['abstract'], public_paper()['abstract'])
        self.assertEqual(proceedings.qualify(value, proceedings.registry()), 'verified')

    def test_real_child_deadline_does_not_block_healthy_source(self):
        real_popen = subprocess.Popen
        children = []
        code = """
import json,pathlib,sys,time
task=json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))
out=pathlib.Path(sys.argv[2])
if task['source']=='dblp': time.sleep(20)
else:
 (out/'result.json').write_text(json.dumps(dict(status='empty',requests=0,records=0)),encoding='utf-8')
"""
        def launch(command, **kwargs):
            process = real_popen([sys.executable, '-c', code, command[-3], command[-1]], **kwargs)
            children.append(process)
            return process
        config = dict(discovery_queries=['world model'], sources=dict(enabled=['dblp','openalex'],
            providers={'dblp': {'deadline_seconds':.3}, 'openalex':{'deadline_seconds':3}}, collection_deadline_seconds=4))
        started = time.monotonic()
        with mock.patch.object(sources.subprocess, 'Popen', side_effect=launch):
            result = sources.discover(self.store, config, [], [2025], 'test')
        self.assertEqual(result['providers']['dblp']['status'], 'timeout')
        self.assertEqual(result['providers']['openalex']['status'], 'empty')
        self.assertLess(time.monotonic()-started, 4)
        self.assertTrue(all(p.poll() is not None for p in children))

    def test_cooldown_does_not_count_old_results_as_live(self):
        self.store.save_source_health('dblp', 'old', dict(status='timeout', stored=100,
            retry_at=(dt.datetime.now(dt.timezone.utc)+dt.timedelta(hours=1)).isoformat()))
        config = dict(discovery_queries=['world model'], sources=dict(enabled=['dblp']))
        with mock.patch.object(sources.subprocess, 'Popen') as popen:
            result = sources.discover(self.store, config, [], [2025], 'new')
        popen.assert_not_called()
        self.assertEqual((result['mode'], result['stored']), ('cached_only', 0))


class AdapterTests(unittest.TestCase):
    def task(self, source):
        return dict(source=source, years=[2025], venue_keys=['icml','nips'], queries=['world model'], states={}, settings={'papers_per_volume':1})

    def test_official_membership_uses_directory_and_page_identity(self):
        html = '<html><div class="paper"><p class="title">World model</p><a href="https://proceedings.mlr.press/v267/test25.html">abs</a></div></html>'
        body = '<meta name="citation_title" content="World model"><meta name="citation_author" content="Ada Author"><div id="abstract">' + 'Evidence for reinforcement learning and planning. '*3 + '</div>'
        runtime = mock.Mock()
        runtime.get.side_effect = [html, body]
        pages = list(source_adapters.official(self.task('pmlr'), runtime))
        self.assertEqual(len(pages), 1)
        self.assertIn('https://proceedings.mlr.press/v267/test25.html', pages[0]['state']['done'])
        self.assertEqual(pages[0]['records'][0]['venue_key'], 'icml')

    def test_directory_resume_skips_completed_pages(self):
        task = self.task('pmlr')
        url='https://proceedings.mlr.press/v267/test25.html'
        task['states'][source_adapters.key('pmlr','https://proceedings.mlr.press/v267/')]={'done':[url]}
        runtime=mock.Mock()
        runtime.get.return_value='<html><a href="'+url+'">Paper</a></html>'
        result=list(source_adapters.official(task,runtime))
        self.assertEqual(runtime.get.call_count,1)
        self.assertTrue(result[0]['state']['exhausted'])

    def test_neurips_workshop_entry_does_not_become_main(self):
        runtime=mock.Mock()
        runtime.get.return_value='<html><li data-track="workshop"><a href="/paper_files/paper/2025/hash/aaaa-Abstract-Conference.html">World model</a></li></html>'
        with self.assertRaises(source_http.SourceError):
            list(source_adapters.official(self.task('neurips'), runtime))

    def test_arxiv_version_and_preprint_status(self):
        runtime=mock.Mock()
        runtime.get.return_value='''<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/cs/9901001v2</id><title>World model</title><published>2025-01-01T00:00:00Z</published><summary>Abstract</summary><author><name>Ada</name></author></entry></feed>'''
        value=list(source_adapters.arxiv(self.task('arxiv'),runtime))[0]['records'][0]
        self.assertEqual(value['external_ids']['arxiv'],'cs/9901001')
        self.assertEqual(value['publication_status'],'preprint')

    def test_openreview_does_not_infer_acceptance_from_venue(self):
        runtime=mock.Mock()
        runtime.json.return_value={'notes':[{'id':'note','forum':'forum','content':{'title':{'value':'World model'},'abstract':{'value':'Text'},'venue':{'value':'ICML 2025'}}}]}
        task=self.task('openreview');task['settings']['venues']=['ICML.cc/2025/Conference']
        value=list(source_adapters.openreview(task,runtime))[0]['records'][0]
        self.assertEqual(value['source_record_id'],'forum')
        self.assertEqual(value['publication_status'],'submission')
        self.assertFalse(value.get('publication_evidence'))

    def test_unknown_response_schema_is_not_empty_success(self):
        runtime=mock.Mock();runtime.json.return_value={}
        with self.assertRaises(source_http.SourceError):
            list(source_adapters.openalex(self.task('openalex'),runtime))


if __name__ == '__main__':
    unittest.main()
