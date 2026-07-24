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

    def test_render_contains_quality_statement(self):
        text = scout.render_report([], 3, 0)
        self.assertIn("CCF-A", text)
        self.assertIn("record-key", text)


if __name__ == "__main__":
    unittest.main()
