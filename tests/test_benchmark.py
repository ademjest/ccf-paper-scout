import copy
import json
import pathlib
import tempfile
import unittest
import urllib.error
from unittest import mock

from ccf_paper_scout import benchmark, official_abstracts, semantic
from ccf_paper_scout.annotation_page import render
from state_store import StateStore


def paper(identifier="conf/icml/Test25", **values):
    return {"id": identifier, "title": "Reinforcement Learning", "authors": ["Ada Smith 0001"],
            "year": 2025, "venue": identifier.split("/")[1], "rank": "A", "doi": "",
            "ee": "https://proceedings.mlr.press/v267/test25.html", "abstract": "",
            "main_conference_status": "verified", "historical_import": True,
            "lexical_score": 0.5, "semantic_score": 0.5, **values}


ABSTRACT = "Reinforcement learning policies learn cooperative decisions with shared rewards and explicit credit assignment."
HTML = '<meta name="citation_title" content="Reinforcement Learning"><meta name="citation_author" content="Ada Smith"><div id="abstract">' + ABSTRACT + '</div>'


class OfficialAbstractTests(unittest.TestCase):
    def test_acl_abstract_heading_is_not_part_of_text_and_year_matches(self):
        document = HTML.replace('<div id="abstract">', '<div class="card-body acl-abstract"><h5 class="card-title">Abstract</h5><span>').replace('</div>', '</span></div>')
        self.assertEqual(official_abstracts.parse_abstract(document, paper()), ABSTRACT)
        url = 'https://aclanthology.org/2025.acl-long.17/'
        self.assertEqual(official_abstracts.official_url(paper(ee=url)), url)
        for wrong in [url.replace('2025', '2024'), url.replace('.org/', '.org.evil/'),
                      url.replace('acl-long', 'findings-acl')]:
            self.assertIsNone(official_abstracts.official_url(paper(ee=wrong)))

    def test_pmlr_and_neurips_page_structures_and_author_order(self):
        self.assertEqual(official_abstracts.parse_abstract(HTML, paper()), ABSTRACT)
        document = HTML.replace('content="Ada Smith"', 'content="Smith, Ada"').replace('<div id="abstract">', '<h2>Abstract</h2><p class="paper-abstract">').replace('</div>', '</p>')
        self.assertEqual(official_abstracts.parse_abstract(document, paper()), ABSTRACT)

    def test_wrong_paper_and_error_page_are_not_abstracts(self):
        for document in [HTML.replace('content="Ada Smith"', 'content="Someone Else"'),
                         HTML.replace('content="Reinforcement Learning"', 'content="Another Paper"'),
                         '<h1>Access denied</h1>', HTML.replace(ABSTRACT, '')]:
            with self.assertRaises(ValueError):
                official_abstracts.parse_abstract(document, paper())

    def test_only_supported_paper_urls_and_years(self):
        for url in ['https://proceedings.mlr.press.evil.test/v267/a.html', 'https://proceedings.mlr.press/v267/',
                    'file:///v267/a.html', 'https://user@proceedings.mlr.press/v267/a.html']:
            self.assertIsNone(official_abstracts.official_url(paper(ee=url)))
        url = 'http://papers.nips.cc/paper_files/paper/2025/hash/abcd-Abstract-Conference.html'
        self.assertTrue(official_abstracts.official_url(paper(ee=url)).startswith('https://proceedings.neurips.cc/'))
        self.assertIsNone(official_abstracts.official_url(paper(ee=url, year=2024)))


class MetadataExperimentTests(unittest.TestCase):
    def test_budget_is_shared_by_fallback_requests(self):
        p = paper(doi='10.1234/test')
        payload = json.dumps({'doi': 'https://doi.org/10.1234/test', 'abstract_inverted_index': {'Evidence': [0]}})
        request = mock.Mock(side_effect=['<h1>Not a paper</h1>', payload])
        stats = benchmark.enrich([p], 2, request, delay=0)
        self.assertEqual(stats['requests'], 2)
        self.assertEqual(p['abstract'], 'Evidence')
        self.assertEqual(len(p['metadata_audit']['attempts']), 2)
        p = paper(doi='10.1234/test')
        request = mock.Mock(return_value='<h1>Not a paper</h1>')
        self.assertEqual(benchmark.enrich([p], 1, request, delay=0)['requests'], 1)
        self.assertFalse(p['abstract'])

    def test_round_robin_venues_and_auditable_missing_reasons(self):
        papers = [paper(f'conf/{v}/{i}', lexical_score=i, semantic_score=10-i) for v in ('aaai','icml','nips') for i in range(4)]
        request = mock.Mock(return_value=HTML)
        stats = benchmark.enrich(papers, 6, request, delay=0)
        self.assertEqual(stats['requests'], 6)
        self.assertEqual({v['abstracts'] for v in stats['by_venue'].values()}, {2})
        self.assertEqual(stats['statuses'], {'not_attempted_budget': 6, 'success': 6})
        self.assertEqual(len({p['id'] for p in benchmark.acquisition_order(papers)}), len(papers))

    def test_no_identifier_and_wrong_doi_never_receive_abstract(self):
        no_id = paper(ee='https://example.org/page')
        wrong_id = paper('conf/aaai/Other', ee='', doi='10.1234/wanted')
        request = mock.Mock(return_value=json.dumps({'doi': 'https://doi.org/10.1234/wrong', 'abstract_inverted_index': {'wrong': [0]}}))
        stats = benchmark.enrich([no_id, wrong_id], 3, request, delay=0)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(no_id['metadata_audit']['status'], 'no_supported_identifier')
        self.assertEqual(wrong_id['metadata_audit']['status'], 'failed')
        self.assertFalse(wrong_id['abstract'])

    def test_rate_limit_blocks_only_that_host(self):
        papers = [paper('conf/aaai/A', ee='', doi='10.1234/a'), paper('conf/aaai/B', ee='', doi='10.1234/b'), paper()]
        def request(url):
            if 'openalex' in url:
                raise urllib.error.HTTPError(url, 429, 'rate limited', {}, None)
            return HTML
        stats = benchmark.enrich(papers, 10, request, delay=0)
        self.assertEqual(stats['requests'], 2)
        self.assertEqual(papers[-1]['abstract'], ABSTRACT)
        self.assertEqual(sum(p['metadata_audit']['status']=='source_throttled' for p in papers), 1)


class FakeEncoder:
    signature = 'benchmark-test-model'
    def encode_query(self, text):
        return [1.0, 0.0]
    def encode_papers(self, papers):
        return [[1.0, 0.0] if 'Reinforcement' in p['title'] else [0.0, 1.0] for p in papers]


class ReplayTests(unittest.TestCase):
    def test_budget_prioritizes_abstracts_and_invalidates_only_changed_text(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(pathlib.Path(directory) / 'test.sqlite3')
            try:
                rows = [paper('conf/icml/A'), paper('conf/icml/B', abstract=ABSTRACT)]
                config = {'explicit_interests': ['reinforcement learning'], 'semantic': {'max_updated_papers_per_run': 1}}
                first = semantic.rank(rows, [], config, store, encoder=FakeEncoder())
                self.assertTrue(rows[0]['semantic_deferred'])
                self.assertEqual(first['modalities']['abstract']['scored'], 1)
                semantic.rank(rows, [], config, store, encoder=FakeEncoder())
                rows[1]['abstract'] += ' Updated.'
                third = semantic.rank(rows, [], config, store, encoder=FakeEncoder())
                self.assertEqual((third['encoded'],third['cache_hits']), (1,1))
                rows[1]['abstract'] += ' Changed again.'
                config['semantic']['max_updated_papers_per_run'] = 0
                semantic.rank(rows, [], config, store, encoder=FakeEncoder())
                self.assertTrue(rows[1]['semantic_deferred'])
                self.assertNotIn('semantic_score', rows[1])
            finally:
                store.close()

    def test_same_snapshot_replays_without_network_or_input_mutation(self):
        example = json.loads((pathlib.Path(__file__).parents[1] / 'config.example.json').read_text(encoding='utf-8'))
        config = benchmark.config_for_experiment(example)
        rows = [paper(abstract=ABSTRACT), paper('conf/icml/Pottery', title='Pottery', abstract='Ancient pottery and ceramics.')]
        original = copy.deepcopy(rows)
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(pathlib.Path(directory) / 'test.sqlite3')
            try:
                with mock.patch('socket.create_connection', side_effect=AssertionError('offline')), mock.patch('paper_scout.send_email', side_effect=AssertionError('no email')):
                    first = benchmark.compare(rows, config, store, encoder=FakeEncoder())
                    second = benchmark.compare(rows, config, store, encoder=FakeEncoder())
                self.assertEqual(first['rankings'], second['rankings'])
                self.assertEqual(second['semantic']['encoded'], 0)
                self.assertEqual(rows, original)
            finally:
                store.close()


class AnnotationTests(unittest.TestCase):
    def result(self):
        papers = [paper(f'conf/icml/P{i}', title=f'Paper {i}', abstract=ABSTRACT) for i in range(40)]
        ids = [p['id'] for p in papers]
        return {'dataset_id': 'test-dataset', 'papers': papers, 'rankings': {'lexical': ids, 'semantic': ids[::-1], 'rrf': ids}}

    def test_grouped_split_deterministic_and_page_is_blind(self):
        result = self.result()
        result['papers'][0]['title'] = result['papers'][1]['title']
        a = benchmark.sample_tasks(result)
        self.assertEqual(a, benchmark.sample_tasks(result))
        self.assertEqual(a['folds']['conf/icml/P0'], a['folds']['conf/icml/P1'])
        a['dataset_id'] = result['dataset_id']
        a['tasks'][0]['title'] = '</script><script>alert(1)</script>'
        page = render(a)
        self.assertNotIn('"folds"', page)
        self.assertNotIn('semantic_score', page)
        self.assertNotIn('</script><script>alert', page)

    def test_missing_labels_are_not_negatives_or_reported_as_zero(self):
        result = self.result()
        manifest = benchmark.sample_tasks(result)
        manifest['dataset_id'] = result['dataset_id']
        evaluated = benchmark.evaluate(result, manifest, {'dataset_id': result['dataset_id'], 'labels': []}, 'test')
        for value in evaluated['methods'].values():
            self.assertIsNone(value['precision@5'])
            self.assertIsNone(value['ndcg@10'])

    def test_test_metrics_ignore_calibration_labels_and_verify_evidence(self):
        result = self.result()
        manifest = benchmark.sample_tasks(result)
        manifest['dataset_id'] = result['dataset_id']
        rows = [{'id': i, 'grade': 2 if fold=='test' else 0, 'evidence': 'cooperative decisions', 'reason': 'Matches my research'} for i,fold in manifest['folds'].items()]
        labels = {'dataset_id': result['dataset_id'], 'labels': rows}
        metrics = benchmark.evaluate(result, manifest, labels, 'test')
        self.assertEqual(metrics['methods']['lexical']['precision@5'], 1)
        self.assertEqual(metrics['methods']['lexical']['ndcg@10'], 1)
        labels['labels'][0]['evidence'] = 'invented quotation'
        with self.assertRaisesRegex(ValueError, 'exact abstract'):
            benchmark.evaluate(result, manifest, labels, 'test')

    def test_mismatched_dataset_duplicate_and_unknown_ids_rejected(self):
        result = self.result()
        manifest = benchmark.sample_tasks(result)
        manifest['dataset_id'] = result['dataset_id']
        for labels in [ {'dataset_id': 'wrong', 'labels': []},
                        {'dataset_id': result['dataset_id'], 'labels': [{'id':'bad','grade':None}]},
                        {'dataset_id': result['dataset_id'], 'labels': [{'id':'conf/icml/P0','grade':None}]*2} ]:
            with self.assertRaises(ValueError):
                benchmark.evaluate(result, manifest, labels, 'test')


if __name__ == '__main__':
    unittest.main()
