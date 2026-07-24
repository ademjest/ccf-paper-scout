import importlib.util
import http.client
import json
import os
import pathlib
import unittest
import urllib.error
from unittest import mock

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

    def test_fetch_zotero_retries_temporary_dns_failure(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        response.read.return_value = json.dumps([]).encode()
        response.status = 200
        response.headers = {}
        failure = urllib.error.URLError(OSError(-3, "Temporary failure in name resolution"))
        with mock.patch.dict(os.environ, {"ZOTERO_USER_ID": "123", "ZOTERO_API_KEY": "secret"}), \
             mock.patch.object(scout.urllib.request, "urlopen", side_effect=[failure, response]) as urlopen, \
             mock.patch.object(scout.time, "sleep"):
            self.assertEqual(scout.fetch_zotero({}, "test-agent"), [])
        self.assertEqual(urlopen.call_count, 2)

    def test_open_json_does_not_retry_forbidden(self):
        request = scout.urllib.request.Request("https://api.zotero.org/users/123/items")
        forbidden = urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)
        with mock.patch.object(scout.urllib.request, "urlopen", side_effect=forbidden) as urlopen, \
             mock.patch.object(scout.time, "sleep") as sleep:
            with self.assertRaises(urllib.error.HTTPError):
                scout.open_json(request)
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()

    def test_fetch_zotero_explains_forbidden_without_leaking_key(self):
        forbidden = urllib.error.HTTPError("https://api.zotero.org/users/123/items", 403, "Forbidden", {}, None)
        with mock.patch.dict(os.environ, {"ZOTERO_USER_ID": "123", "ZOTERO_API_KEY": "super-secret"}), \
             mock.patch.object(scout, "open_json", side_effect=forbidden):
            with self.assertRaisesRegex(RuntimeError, "Zotero refused access.*user ID.*read access") as raised:
                scout.fetch_zotero({}, "test-agent")
        self.assertNotIn("super-secret", str(raised.exception))

    def test_open_json_retries_remote_disconnect(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        response.read.return_value = json.dumps({"ok": True}).encode()
        response.status = 200
        response.headers = {}
        disconnected = http.client.RemoteDisconnected("Remote end closed connection without response")
        with mock.patch.object(scout.urllib.request, "urlopen", side_effect=[disconnected, response]) as urlopen, \
             mock.patch.object(scout.time, "sleep") as sleep:
            self.assertEqual(scout.open_json("https://dblp.org/test"), {"ok": True})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_fetch_dblp_falls_back_to_uni_trier_mirror(self):
        venue = {"abbr": "NeurIPS", "dblp_key": "nips", "type": "conference", "name": "NeurIPS", "rank": "A"}
        payload = {"result": {"hits": {"hit": [{"info": {
            "key": "conf/nips/Test25", "title": "Test Paper", "year": "2025",
            "authors": {"author": {"text": "Ada Author"}},
            "url": "https://dblp.org/rec/conf/nips/Test25"
        }}]}}}
        with mock.patch.object(scout, "request_json", side_effect=[RuntimeError("primary disconnected"), payload]) as request_json:
            papers = scout.fetch_dblp(venue, 2025, 1, "test-agent")
        self.assertEqual(papers[0]["id"], "conf/nips/Test25")
        self.assertEqual(request_json.call_count, 2)
        self.assertIn("https://dblp.org/", request_json.call_args_list[0].args[0])
        self.assertIn("https://dblp.uni-trier.de/", request_json.call_args_list[1].args[0])

    def test_ranking_reasons_exclude_generic_academic_words(self):
        interests = [{"title": "LLM Agent", "abstract": "We propose a framework which can provide robust results."}]
        candidates = [{"title": "Agent Planning", "abstract": "We propose a framework which can provide agent results.", "year": 2026}]
        ranked = scout.rank_candidates(interests, candidates, ["agent"])
        self.assertEqual(ranked[0]["reasons"], ["agent"])

    def test_explicit_interests_boost_matching_candidate(self):
        interests = [{"title": "image classification", "abstract": "computer vision"}]
        candidates = [
            {"title": "Autonomous LLM Agents with Reinforcement Learning", "abstract": "agent planning", "year": 2026},
            {"title": "Image Segmentation with Vision Transformers", "abstract": "pixel labels", "year": 2026},
        ]
        ranked = scout.rank_candidates(interests, candidates, ["reinforcement learning", "large language models", "autonomous agents"])
        self.assertEqual(ranked[0]["title"], "Autonomous LLM Agents with Reinforcement Learning")
        self.assertIn("reinforcement", ranked[0]["reasons"])

    def test_format_zotero_debug_lists_every_item(self):
        papers = [
            {"key": "A1", "title": "First Paper", "abstract": "Abstract one", "dateAdded": "2026-01-01", "itemType": "journalArticle"},
            {"key": "B2", "title": "Second Paper", "abstract": "", "dateAdded": "2026-01-02", "itemType": "conferencePaper"},
        ]
        text = scout.format_zotero_debug(papers)
        self.assertIn("共读取 2 篇", text)
        self.assertIn("1. [journalArticle] First Paper", text)
        self.assertIn("2. [conferencePaper] Second Paper", text)
        self.assertIn("Key: B2", text)

    def test_llm_status_explains_disabled_even_when_endpoint_is_configured(self):
        config = {"enabled": False, "base_url": "https://llm.example/v1", "model": "demo", "api_key_env": "LLM_API_KEY"}
        with mock.patch.dict(os.environ, {"LLM_API_KEY": "secret"}):
            status = scout.llm_status(config)
        self.assertIn("disabled", status)
        self.assertIn("enabled=true", status)

    def test_filter_unseen_reports_and_excludes_seen_ids(self):
        candidates = {
            "conf/nips/Old25": {"id": "conf/nips/Old25"},
            "conf/nips/New25": {"id": "conf/nips/New25"},
        }
        unseen, skipped = scout.filter_unseen(candidates, {"conf/nips/Old25"})
        self.assertEqual(list(unseen), ["conf/nips/New25"])
        self.assertEqual(skipped, 1)

    def test_translate_papers_calls_openai_compatible_api(self):
        papers = [{"id": "p1", "title": "Agent Learning", "abstract": "An agent learns."}]
        config = {"enabled": True, "base_url": "https://llm.example/v1", "model": "demo", "language": "简体中文"}
        response = {"choices": [{"message": {"content": json.dumps({
            "title_zh": "智能体学习", "abstract_zh": "一个智能体进行学习。"
        }, ensure_ascii=False)}}]}
        with mock.patch.dict(os.environ, {"LLM_API_KEY": "secret"}), \
             mock.patch.object(scout, "post_json", return_value=response) as post_json:
            scout.translate_papers(papers, config, "test-agent")
        self.assertEqual(papers[0]["title_zh"], "智能体学习")
        self.assertEqual(papers[0]["abstract_zh"], "一个智能体进行学习。")
        request = post_json.call_args.args[0]
        self.assertEqual(request.full_url, "https://llm.example/v1/chat/completions")
        self.assertNotIn("secret", str(post_json.call_args))

    def test_translate_papers_uses_cache_without_api_call(self):
        papers = [{"id": "p1", "title": "Agent Learning", "abstract": "An agent learns."}]
        config = {"enabled": True, "base_url": "https://llm.example/v1", "model": "demo"}
        with self.subTest("cached translation"):
            import tempfile
            with tempfile.TemporaryDirectory() as directory:
                cache_path = pathlib.Path(directory) / "translations.json"
                cache_path.write_text(json.dumps({"p1": {"title_zh": "智能体学习", "abstract_zh": "缓存摘要"}}), encoding="utf-8")
                with mock.patch.dict(os.environ, {"LLM_API_KEY": "secret"}), \
                     mock.patch.object(scout, "post_json") as post_json:
                    scout.translate_papers(papers, config, "test-agent", cache_path)
                post_json.assert_not_called()
                self.assertEqual(papers[0]["title_zh"], "智能体学习")
                self.assertEqual(papers[0]["abstract_zh"], "缓存摘要")

    def test_translate_papers_returns_api_and_cache_counts(self):
        papers = [
            {"id": "cached", "title": "Cached", "abstract": ""},
            {"id": "fresh", "title": "Fresh", "abstract": "Fresh abstract"},
        ]
        config = {"enabled": True, "base_url": "https://llm.example/v1", "model": "demo"}
        response = {"choices": [{"message": {"content": json.dumps({"title_zh": "新论文", "abstract_zh": "新摘要"}, ensure_ascii=False)}}]}
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            cache_path = pathlib.Path(directory) / "translations.json"
            cache_path.write_text(json.dumps({"cached": {"title_zh": "缓存", "abstract_zh": "缓存摘要"}}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"LLM_API_KEY": "secret"}), mock.patch.object(scout, "post_json", return_value=response):
                counts = scout.translate_papers(papers, config, "test-agent", cache_path)
        self.assertEqual(counts, (1, 1))

    def test_render_report_includes_translations_and_abstract(self):
        paper = {"title": "Agent Learning", "title_zh": "智能体学习", "abstract": "English abstract.",
                 "abstract_zh": "中文摘要。", "score": 1.0, "venue": "AAAI", "rank": "A", "year": 2026,
                 "type": "conference", "authors": ["Ada"], "reasons": ["agent"], "url": "https://dblp.org/x", "ee": ""}
        text = scout.render_report([paper], 1, 1)
        self.assertIn("中文标题：智能体学习", text)
        self.assertIn("中文摘要：中文摘要。", text)
        self.assertIn("原文摘要：English abstract.", text)

    def test_render_contains_quality_statement(self):
        text = scout.render_report([], 3, 0)
        self.assertIn("CCF-A", text)
        self.assertIn("record-key", text)


if __name__ == "__main__":
    unittest.main()
