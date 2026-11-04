import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from ccf_paper_scout import search, search_evaluation, search_history, semantic
from state_store import StateStore


class Encoder:
    signature = 'search-test-v1'

    def __init__(self):
        self.encoded = []

    def encode_query(self, text):
        return [1.0, 0.0]

    def encode_papers(self, papers):
        self.encoded.extend(copy.deepcopy(papers))
        return [[1.0, 0.0] if 'dynamics' in p.get('abstract', '') else [0.0, 1.0] for p in papers]


class SearchSemanticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'search.sqlite3'
        self.store = StateStore(self.path)
        self.encoder = Encoder()
        self.papers = [
            dict(id='a', title='Learning to imagine', abstract='Learned dynamics guide decisions. Agents predict future observations and choose actions through imagined trajectories.', year=2025),
            dict(id='b', title='Planning a library catalog', abstract='Indexing historical books. We organize bibliographic records by author and publication date for academic libraries.', year=2025),
        ]

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def rank(self, papers=None, mode='hybrid', **kwargs):
        with mock.patch.object(search, 'local_encoder', return_value=self.encoder):
            return search.rank_results(self.store, copy.deepcopy(papers or self.papers), 'planning', mode, **kwargs)

    def test_vector_retrieval_admits_abstract_match_without_keyword_overlap(self):
        result = self.rank()
        self.assertIn('a', [p['id'] for p in result])
        abstract_match = next(p for p in result if p['id'] == 'a')
        self.assertEqual(abstract_match['lexical_score'], 0)
        self.assertEqual(abstract_match['semantic_score'], 1)
        self.assertEqual(self.rank(mode='semantic')[0]['id'], 'a')
        self.assertEqual([p['id'] for p in self.rank(mode='lexical')], ['b'])
        self.assertIn('dynamics', self.encoder.encoded[0]['abstract'])

    def test_vector_cache_reused_and_invalidated_after_abstract_arrives(self):
        rows = copy.deepcopy(self.papers)
        rows[0]['abstract'] = ''
        first, second, third = {}, {}, {}
        self.rank(rows, diagnostics=first)
        self.rank(rows, diagnostics=second)
        self.rank(diagnostics=third)
        self.assertEqual(first['encoded'], 2)
        self.assertEqual(second['cache_hits'], 2)
        self.assertEqual(second['encoded'], 0)
        self.assertEqual(third['cache_hits'], 1)
        self.assertEqual(third['encoded'], 1)

    def test_failure_is_explicit_and_does_not_download_model(self):
        details = {}
        with mock.patch.object(search, 'local_encoder', side_effect=OSError('offline')) as load:
            result = search.rank_results(self.store, self.papers, 'planning', 'hybrid', diagnostics=details)
        self.assertTrue(json.loads(load.call_args.args[0])['local_files_only'])
        self.assertEqual(details['applied'], 'lexical')
        self.assertEqual(details['fallback'], 'local_model_unavailable')
        self.assertEqual([p['id'] for p in result], ['b'])

    def test_budget_reports_partial_coverage_and_preserves_lexical_results(self):
        details = {}
        result = self.rank(settings={'max_updated_papers_per_run': 1}, diagnostics=details)
        self.assertEqual(details['deferred'], 1)
        self.assertEqual({p['id'] for p in result}, {'a', 'b'})
        self.assertTrue(next(p for p in result if p['id'] == 'b')['semantic_deferred'])

    def test_cancel_stops_between_embedding_batches(self):
        event = threading.Event()
        encode = self.encoder.encode_papers
        def stop(papers):
            result = encode(papers)
            event.set()
            return result
        with mock.patch.object(self.encoder, 'encode_papers', side_effect=stop):
            stats = semantic.rank(self.papers, [], {'semantic': {'batch_size': 1}, 'explicit_interests': ['planning']},
                                  self.store, encoder=self.encoder, cancel=event)
        self.assertEqual(stats['encoded'], 1)
        self.assertEqual(stats['deferred'], 1)

    def test_pipeline_filters_before_embedding_and_preserves_history(self):
        for index, paper in enumerate(self.papers):
            self.store.save_source_page('openalex', dict(task_key=str(index), records=[dict(paper,
                source_record_id=str(index), publication_status='published', publication_type='journal-article')], state={}))
        self.store.save_source_page('arxiv', dict(task_key='preprint', records=[dict(self.papers[0],
            id='preprint', title='Preprint about planning', publication_status='preprint', source_record_id='preprint')], state={}))
        states = []
        with mock.patch.object(search, 'local_encoder', return_value=self.encoder):
            result = search.run({'years':[2025], 'venue_keys':[]}, self.path, 'planning', cached_only=True,
                                ranking='hybrid', update=lambda r: states.append(r['state']), require_abstract=True)
        self.assertEqual(states, ['cached', 'ranking'])
        self.assertEqual(result['ranking'], 'hybrid')
        self.assertEqual(result['request']['ranking'], 'hybrid')
        self.assertEqual(len(result['results']), 2)
        self.assertNotIn('Preprint about planning', [p['title'] for p in self.encoder.encoded])
        restored = search_history.get(self.store, result['id'])
        self.assertEqual(restored['ranking_details'], result['ranking_details'])
        exported = search_evaluation.export(self.store, [result['id']])
        search_evaluation.validate(exported)
        self.assertEqual(exported['queries'][0]['ranking_method'], 'hybrid')
        self.assertEqual(self.store.delivered_ids(), set())

    def test_invalid_mode_rejected_before_session_creation(self):
        with self.assertRaises(ValueError):
            search.run({'years':[2025], 'venue_keys':[]}, self.path, 'planning', ranking='invalid', cached_only=True)
        self.assertEqual(search_history.recent(self.store), [])

    def test_long_abstract_is_chunked_with_title_in_each_chunk(self):
        model = mock.Mock(max_seq_length=32)
        model.tokenizer.encode.side_effect = lambda text, **_: text.split()
        model.tokenizer.decode.side_effect = lambda tokens, **_: ' '.join(tokens)
        model.encode.side_effect = lambda texts, **_: [[1.0, 0.0] for _ in texts]
        encoder = semantic.Encoder({}, model=model)
        encoder.encode_papers([dict(title='Paper title', abstract=' '.join('word'+str(i) for i in range(40)))])
        texts = model.encode.call_args.args[0]
        self.assertEqual(len(texts), 2)
        self.assertTrue(all(text.startswith('Paper title\n') for text in texts))
        self.assertIn('word39', texts[-1])


if __name__ == '__main__':
    unittest.main()
