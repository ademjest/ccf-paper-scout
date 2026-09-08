import email.message
import importlib.util
import json
import os
import pathlib
import sys
import types
import unittest
import urllib.error
import urllib.parse
from unittest import mock


if os.name == "nt":
    fcntl = types.ModuleType("fcntl")
    fcntl.LOCK_EX = 1
    fcntl.LOCK_NB = 2
    fcntl.LOCK_UN = 8
    fcntl.flock = lambda *_args: None
    sys.modules.setdefault("fcntl", fcntl)

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("paper_scout", ROOT / "paper_scout.py")
scout = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(scout)


class FakeResponse:
    def __init__(self, body, content_type="application/json"):
        self.body = body.encode("utf-8") if isinstance(body, str) else body
        self.headers = email.message.Message()
        self.headers["Content-Type"] = content_type

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def venue(key="nips", abbr="NeurIPS"):
    return {
        "abbr": abbr,
        "dblp_key": key,
        "type": "conference",
        "name": abbr,
        "rank": "A",
    }


def sparql_payload(record="conf/nips/Test25"):
    return {
        "results": {
            "bindings": [
                {
                    "publication": {"value": f"https://dblp.org/rec/{record}"},
                    "title": {"value": "A Resilient Paper."},
                    "year": {"value": "2025"},
                    "doi": {"value": "https://doi.org/10.1000/TEST.25"},
                    "document": {"value": "https://example.org/paper"},
                    "authors": {"value": "Ada Author|||Bob Researcher"},
                }
            ]
        }
    }


class DblpResilienceTests(unittest.TestCase):
    def setUp(self):
        scout._DBLP_LAST_REQUEST_AT = 0.0
        scout._DBLP_SEARCH_API_BLOCKED = False

    def test_anti_bot_html_is_not_retried(self):
        response = FakeResponse(
            '<!doctype html><title>Making sure you\'re not a bot!</title>',
            "text/html; charset=utf-8",
        )
        with mock.patch.object(scout.urllib.request, "urlopen", return_value=response) as urlopen, \
             mock.patch.object(scout.time, "sleep") as sleep:
            with self.assertRaisesRegex(scout.DblpBlockedError, "anti-bot challenge"):
                scout.request_dblp_json("https://dblp.org/search/publ/api", "test", attempts=5)
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()

    def test_unexpected_html_is_a_non_retryable_protocol_error(self):
        response = FakeResponse("<html>maintenance</html>", "text/html")
        with mock.patch.object(scout.urllib.request, "urlopen", return_value=response) as urlopen:
            with self.assertRaisesRegex(scout.DblpProtocolError, "text/html instead of JSON"):
                scout.request_dblp_json("https://dblp.org/search/publ/api", "test", attempts=5)
        self.assertEqual(urlopen.call_count, 1)

    def test_transient_server_error_is_retried(self):
        url = "https://sparql.dblp.org/sparql"
        unavailable = urllib.error.HTTPError(url, 503, "Unavailable", {}, None)
        response = FakeResponse(json.dumps({"ok": True}))
        with mock.patch.object(scout.urllib.request, "urlopen", side_effect=[unavailable, response]) as urlopen, \
             mock.patch.object(scout.time, "sleep"):
            payload = scout.request_dblp_json(url, "test", attempts=2)
        self.assertEqual(payload, {"ok": True})
        self.assertEqual(urlopen.call_count, 2)

    def test_process_wide_throttle_waits_between_requests(self):
        scout._DBLP_LAST_REQUEST_AT = 10.0
        with mock.patch.object(scout.time, "monotonic", side_effect=[10.5, 12.0]), \
             mock.patch.object(scout.time, "sleep") as sleep:
            scout._wait_for_dblp_slot(2.0)
        sleep.assert_called_once_with(1.5)
        self.assertEqual(scout._DBLP_LAST_REQUEST_AT, 12.0)

    def test_sparql_page_maps_publication_metadata(self):
        with mock.patch.object(scout, "request_dblp_json", return_value=sparql_payload()) as request_json:
            papers, total, raw_count = scout.fetch_dblp_sparql_page(
                venue(), 2025, 10, 0, "test-agent"
            )
        self.assertEqual(raw_count, 1)
        self.assertEqual(total, 1)
        self.assertEqual(papers[0]["id"], "conf/nips/Test25")
        self.assertEqual(papers[0]["authors"], ["Ada Author", "Bob Researcher"])
        self.assertEqual(papers[0]["doi"], "10.1000/test.25")
        url = request_json.call_args.args[0]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["query"][0]
        self.assertIn("dblp:Inproceedings", query)
        self.assertIn("https://dblp.org/streams/conf/nips", query)
        self.assertIn('"2025"^^xsd:gYear', query)

    def test_search_api_success_keeps_existing_mapping(self):
        payload = {
            "result": {
                "hits": {
                    "@total": "1",
                    "hit": [
                        {
                            "info": {
                                "key": "conf/nips/Search25",
                                "title": "Search API Paper.",
                                "year": "2025",
                                "authors": {"author": {"text": "Ada Author"}},
                                "url": "https://dblp.org/rec/conf/nips/Search25",
                            }
                        }
                    ],
                }
            }
        }
        with mock.patch.object(scout, "request_dblp_json", return_value=payload) as request_json:
            papers, total, raw_count = scout.fetch_dblp_page(venue(), 2025, 10, 0, "test")
        self.assertEqual(total, 1)
        self.assertEqual(raw_count, 1)
        self.assertEqual(papers[0]["id"], "conf/nips/Search25")
        self.assertEqual(papers[0]["authors"], ["Ada Author"])
        self.assertEqual(request_json.call_count, 1)
        self.assertIn("dblp.org/search/publ/api", request_json.call_args.args[0])

    def test_search_block_switches_to_sparql_and_opens_circuit(self):
        blocked = scout.DblpBlockedError("challenge")
        responses = [blocked, blocked, sparql_payload(), sparql_payload("conf/nips/Second25")]
        with mock.patch.object(scout, "request_dblp_json", side_effect=responses) as request_json:
            first, _, _ = scout.fetch_dblp_page(venue(), 2025, 10, 0, "test")
            second, _, _ = scout.fetch_dblp_page(venue(), 2025, 10, 10, "test")
        self.assertEqual(first[0]["id"], "conf/nips/Test25")
        self.assertEqual(second[0]["id"], "conf/nips/Second25")
        self.assertTrue(scout._DBLP_SEARCH_API_BLOCKED)
        self.assertEqual(request_json.call_count, 4)
        self.assertIn("sparql.dblp.org", request_json.call_args_list[2].args[0])
        self.assertIn("sparql.dblp.org", request_json.call_args_list[3].args[0])

    def test_open_search_circuit_makes_sparql_failure_global(self):
        scout._DBLP_SEARCH_API_BLOCKED = True
        failure = scout.DblpRequestError("SPARQL unavailable")
        with mock.patch.object(scout, "request_dblp_json", side_effect=failure) as request_json:
            with self.assertRaises(scout.DblpUnavailableError) as raised:
                scout.fetch_dblp_page(venue(), 2025, 10, 0, "test")
        self.assertTrue(raised.exception.global_failure)
        self.assertEqual(request_json.call_count, 1)
        self.assertIn("sparql.dblp.org", request_json.call_args.args[0])

    def test_global_endpoint_failure_stops_remaining_partitions(self):
        failure = scout.DblpUnavailableError("blocked and fallback failed", global_failure=True)
        venues = {"nips": venue(), "icml": venue("icml", "ICML")}
        config = {"failure_policy": "continue", "minimum_success_ratio": 0.75}
        identities = {"dois": set(), "titles": set(), "arxiv_ids": set(), "dblp_ids": set()}
        with mock.patch.object(scout, "fetch_dblp_incremental", side_effect=failure) as fetch:
            with self.assertRaisesRegex(RuntimeError, "globally unavailable after search and SPARQL fallback"):
                scout.collect_dblp_sources(
                    ["nips", "icml"], [2026, 2025], venues, config, "test", set(), identities
                )
        self.assertEqual(fetch.call_count, 1)


if __name__ == "__main__":
    unittest.main()
