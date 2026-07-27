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

    def test_translate_papers_adds_structured_focus_fields(self):
        papers = [{"id": "p1", "title": "Agent Learning", "abstract": "An agent learns with tools."}]
        config = {"enabled": True, "base_url": "https://llm.example/v1", "model": "demo", "language": "简体中文"}
        response = {"choices": [{"message": {"content": json.dumps({
            "title_zh": "智能体学习", "abstract_zh": "智能体借助工具学习。",
            "focus": "工具增强智能体学习", "problem": "如何让智能体有效使用工具",
            "method": "联合训练策略与工具调用", "novelty": "统一学习和调用决策",
            "evidence": "在多项工具任务上评估", "limitations": "仅在摘要披露范围内",
            "why_relevant": "与 LLM Agent 和强化学习方向相关", "tags": ["LLM Agent", "RL", "Tool Use"]
        }, ensure_ascii=False)}}]}
        with mock.patch.dict(os.environ, {"LLM_API_KEY": "secret"}), mock.patch.object(scout, "post_json", return_value=response):
            scout.translate_papers(papers, config, "test-agent")
        self.assertEqual(papers[0]["focus"], "工具增强智能体学习")
        self.assertEqual(papers[0]["tags"], ["LLM Agent", "RL", "Tool Use"])

    def test_analysis_cache_fingerprint_changes_with_interests_and_model(self):
        paper = {"id": "p1", "title": "Agent", "abstract": "Tools"}
        base = {"model": "m1", "language": "简体中文", "user_interests": ["agents"]}
        original = scout.analysis_fingerprint(paper, base)
        self.assertNotEqual(original, scout.analysis_fingerprint(paper, {**base, "model": "m2"}))
        self.assertNotEqual(original, scout.analysis_fingerprint(paper, {**base, "user_interests": ["RL"]}))
        self.assertNotEqual(original, scout.analysis_fingerprint({**paper, "abstract": "Changed"}, base))

    def test_analysis_payload_rejects_missing_fields_and_bad_tags(self):
        with self.assertRaisesRegex(ValueError, "focus"):
            scout.validate_analysis_payload({"title_zh": "标题", "abstract_zh": "摘要"})
        complete = {field: field for field in scout.ANALYSIS_STRING_FIELDS}
        with self.assertRaisesRegex(ValueError, "2-5"):
            scout.validate_analysis_payload({**complete, "tags": ["only-one"]})

    def test_atomic_write_text_replaces_complete_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "state.json"
            path.write_text("old", encoding="utf-8")
            scout.atomic_write_text(path, "new content")
            self.assertEqual(path.read_text(encoding="utf-8"), "new content")
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_seen_update_requires_success_when_delivery_enabled(self):
        self.assertTrue(scout.should_update_seen([{"id": "p1"}], False, False, False))
        self.assertTrue(scout.should_update_seen([{"id": "p1"}], True, True, False))
        self.assertFalse(scout.should_update_seen([{"id": "p1"}], True, False, False))
        self.assertFalse(scout.should_update_seen([], True, True, False))
        self.assertFalse(scout.should_update_seen([{"id": "p1"}], True, True, True))

    def test_send_email_uses_smtp_ssl_and_returns_success(self):
        config = {"enabled": True, "host": "smtp.example.com", "port": 465, "sender": "a@example.com", "receiver": "b@example.com", "password_env": "SMTP_PASSWORD", "use_ssl": True}
        smtp = mock.MagicMock()
        smtp.__enter__.return_value = smtp
        smtp.__exit__.return_value = False
        with mock.patch.dict(os.environ, {"SMTP_PASSWORD": "secret"}), mock.patch.object(scout.smtplib, "SMTP_SSL", return_value=smtp):
            receipt = scout.send_email("Daily papers", "# Report", config)
        self.assertTrue(receipt)
        smtp.login.assert_called_once_with("a@example.com", "secret")
        smtp.send_message.assert_called_once()

    def test_delivery_disabled_does_not_claim_success(self):
        self.assertFalse(scout.send_email("Daily papers", "# Report", {"enabled": False}))

    def test_render_report_includes_focus_cards(self):
        paper = {"title": "Agent Learning", "title_zh": "智能体学习", "abstract": "English abstract.",
                 "abstract_zh": "中文摘要。", "focus": "工具增强智能体", "problem": "工具选择",
                 "method": "联合学习", "novelty": "统一策略", "evidence": "多任务评测", "limitations": "摘要未报告更多限制",
                 "why_relevant": "命中 Agent/RL", "tags": ["Agent", "RL"], "score": 1.0, "venue": "AAAI", "rank": "A", "year": 2026,
                 "type": "conference", "authors": ["Ada"], "reasons": ["agent"], "url": "https://dblp.org/x", "ee": ""}
        text = scout.render_report([paper], 1, 1)
        for expected in ["论文聚焦：工具增强智能体", "解决问题：工具选择", "核心方法：联合学习", "主要创新：统一策略", "证据/实验：多任务评测", "局限提示：摘要未报告更多限制", "为何推荐：命中 Agent/RL", "主题标签：Agent、RL"]:
            self.assertIn(expected, text)

    def test_translate_papers_calls_openai_compatible_api(self):
        papers = [{"id": "p1", "title": "Agent Learning", "abstract": "An agent learns."}]
        config = {"enabled": True, "base_url": "https://llm.example/v1", "model": "demo", "language": "简体中文"}
        response = {"choices": [{"message": {"content": json.dumps({
            "title_zh": "智能体学习", "abstract_zh": "一个智能体进行学习。",
            "focus": "智能体学习", "problem": "如何学习", "method": "训练策略",
            "novelty": "统一训练", "evidence": "摘要未披露", "limitations": "摘要未披露",
            "why_relevant": "与 Agent 相关", "tags": ["Agent", "Learning"]
        }, ensure_ascii=False)}}]}
        with mock.patch.dict(os.environ, {"LLM_API_KEY": "secret"}), \
             mock.patch.object(scout, "post_json", return_value=response) as post_json:
            scout.translate_papers(papers, config, "test-agent")
        self.assertEqual(papers[0]["title_zh"], "智能体学习")
        self.assertEqual(papers[0]["abstract_zh"], "一个智能体进行学习。")
        request = post_json.call_args.args[0]
        self.assertEqual(request.full_url, "https://llm.example/v1/chat/completions")
        self.assertNotIn("secret", str(post_json.call_args))

    def test_legacy_translation_cache_without_focus_is_refreshed(self):
        papers = [{"id": "p1", "title": "Agent Learning", "abstract": "An agent learns."}]
        config = {"enabled": True, "base_url": "https://llm.example/v1", "model": "demo"}
        response = {"choices": [{"message": {"content": json.dumps({
            "title_zh": "智能体学习", "abstract_zh": "摘要", "focus": "智能体学习",
            "problem": "问题", "method": "方法", "novelty": "创新", "evidence": "证据",
            "limitations": "局限", "why_relevant": "相关", "tags": ["Agent", "Learning"]
        }, ensure_ascii=False)}}]}
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            cache_path = pathlib.Path(directory) / "translations.json"
            cache_path.write_text(json.dumps({"p1": {"title_zh": "旧翻译", "abstract_zh": "旧摘要"}}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"LLM_API_KEY": "secret"}), mock.patch.object(scout, "post_json", return_value=response) as post_json:
                scout.translate_papers(papers, config, "test-agent", cache_path)
            self.assertEqual(post_json.call_count, 1)
            self.assertEqual(papers[0]["focus"], "智能体学习")

    def test_translate_papers_uses_cache_without_api_call(self):
        papers = [{"id": "p1", "title": "Agent Learning", "abstract": "An agent learns."}]
        config = {"enabled": True, "base_url": "https://llm.example/v1", "model": "demo"}
        with self.subTest("cached translation"):
            import tempfile
            with tempfile.TemporaryDirectory() as directory:
                cache_path = pathlib.Path(directory) / "translations.json"
                cached = {"title_zh": "智能体学习", "abstract_zh": "缓存摘要", "focus": "缓存聚焦", "problem": "缓存问题", "method": "缓存方法", "novelty": "缓存创新", "evidence": "缓存证据", "limitations": "缓存局限", "why_relevant": "缓存相关", "tags": ["Agent", "RL"]}
                cached["_fingerprint"] = scout.analysis_fingerprint(papers[0], config)
                cache_path.write_text(json.dumps({"p1": cached}), encoding="utf-8")
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
        response = {"choices": [{"message": {"content": json.dumps({"title_zh": "新论文", "abstract_zh": "新摘要", "focus": "新聚焦", "problem": "新问题", "method": "新方法", "novelty": "新创新", "evidence": "新证据", "limitations": "新局限", "why_relevant": "新相关", "tags": ["Agent", "RL"]}, ensure_ascii=False)}}]}
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            cache_path = pathlib.Path(directory) / "translations.json"
            cached = {"title_zh": "缓存", "abstract_zh": "缓存摘要", "focus": "缓存聚焦", "problem": "缓存问题", "method": "缓存方法", "novelty": "缓存创新", "evidence": "缓存证据", "limitations": "缓存局限", "why_relevant": "缓存相关", "tags": ["Agent", "RL"]}
            cached["_fingerprint"] = scout.analysis_fingerprint(papers[0], config)
            cache_path.write_text(json.dumps({"cached": cached}), encoding="utf-8")
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

    def test_build_venue_data_parser_extracts_javascript_mapping(self):
        from scripts import build_venue_data
        text = 'ccf.rankUrl = {\n  "/conf/nips/nips": "A",\n  "/conf/test/test": "B",\n};'
        self.assertEqual(build_venue_data.parse_mapping(text), {"/conf/nips/nips": "A", "/conf/test/test": "B"})

    def test_venue_data_has_unique_runtime_keys(self):
        data = json.loads((ROOT / "data" / "ccf_a_venues.json").read_text(encoding="utf-8"))
        keys = [(venue["type"], venue["dblp_key"].lower()) for venue in data["venues"]]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(data["source_commit"], "540396b36bfb46b18cfed22bf5c578d73257c4b9")

    def test_openalex_not_found_is_best_effort(self):
        papers = [
            {"id": "missing", "title": "Missing", "ee": "https://doi.org/10.1/missing"},
            {"id": "found", "title": "Found", "ee": "https://doi.org/10.1/found"},
        ]
        not_found = urllib.error.HTTPError("https://api.openalex.org/x", 404, "Not Found", {}, None)
        payload = {"id": "W1", "abstract_inverted_index": {"useful": [0], "abstract": [1]}}
        with mock.patch.object(scout, "request_json", side_effect=[not_found, payload]):
            stats = scout.enrich_candidates(papers, 1, "test-agent")
        self.assertEqual(stats, (2, 1, 1, 0))
        self.assertEqual(papers[1]["abstract"], "useful abstract")

    def test_render_contains_quality_statement(self):
        text = scout.render_report([], 3, 0)
        self.assertIn("CCF-A", text)
        self.assertIn("record-key", text)


if __name__ == "__main__":
    unittest.main()
