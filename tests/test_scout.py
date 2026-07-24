import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("paper_scout", ROOT / "paper_scout.py")
scout = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(scout)


class ScoutTests(unittest.TestCase):
    def test_tokenize_filters_stopwords(self):
        self.assertEqual(scout.tokenize("The Retrieval-Augmented Generation Method"), ["retrieval-augmented", "generation"])

    def test_rank_prefers_matching_title(self):
        interests = [{"title": "Retrieval augmented generation", "abstract": "dense retrieval for language generation"}]
        candidates = [
            {"title": "Retrieval Augmented Language Generation", "year": 2026},
            {"title": "Cache Coherence in Multiprocessors", "year": 2026},
        ]
        ranked = scout.rank_candidates(interests, candidates)
        self.assertEqual(ranked[0]["title"], "Retrieval Augmented Language Generation")
        self.assertGreater(ranked[0]["score"], ranked[1]["score"])

    def test_openalex_abstract_reconstructs_order(self):
        abstract = scout.openalex_abstract({"world": [1], "hello": [0]})
        self.assertEqual(abstract, "hello world")

    def test_render_contains_quality_statement(self):
        text = scout.render_report([], 3, 0)
        self.assertIn("CCF-A", text)
        self.assertIn("record-key", text)


if __name__ == "__main__":
    unittest.main()
