import json
import pathlib
import tempfile
import unittest
import urllib.error
from unittest import mock

import paper_scout
from state_store import StateStore
from ccf_paper_scout import metadata, official_abstracts
from scripts.build_actions_config import build

ROOT = pathlib.Path(__file__).resolve().parents[1]
ABSTRACT = "An agent learns long-horizon control policies by planning in an action-conditioned latent world model."
HTML = '<meta name="citation_title" content="World Model"><meta name="citation_author" content="Ada Smith"><div id="abstract">' + ABSTRACT + '</div>'
CITATION = "Ada Smith, John Doe. Proceedings of the 63rd Annual Meeting of the Association for Computational Linguistics (Volume 1: Long Papers). 2025."


def paper(identifier="conf/icml/Test25", **fields):
    return {"id": identifier, "title": "World Model", "authors": ["Ada Smith"], "year": 2025,
            "ee": "https://proceedings.mlr.press/v267/test25.html", "doi": "10.1234/test", "abstract": "", **fields}


class DailyRolloutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.store = StateStore(self.root / "state.sqlite3")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def enrich(self, papers, budget, request=None, fetch=None):
        self.store.save_discovery_page("icml", 2025, papers, {})
        with mock.patch.object(metadata.time, "sleep"):
            return metadata.enrich(papers, self.store, {"max_requests_per_run": budget}, "test",
                                   request or mock.Mock(), paper_scout.normalize_doi, paper_scout.openalex_abstract,
                                   fetch_text=fetch or mock.Mock(return_value=HTML))

    def test_official_abstract_does_not_require_doi(self):
        p = paper(doi="")
        request = mock.Mock()
        result = self.enrich([p], 1, request=request)
        self.assertEqual(result["enriched"], 1)
        self.assertEqual(p["abstract"], ABSTRACT)
        request.assert_not_called()

    def test_fallback_shares_budget_and_checks_doi(self):
        for budget in (1, 2):
            p = paper(identifier=f"conf/icml/B{budget}")
            response = {"doi": "https://doi.org/10.1234/test", "abstract_inverted_index": {"Evidence": [0]}}
            request = mock.Mock(return_value=response)
            result = self.enrich([p], budget, request, mock.Mock(return_value=HTML.replace('content="World Model"', 'content="Other"')))
            self.assertEqual(result["requests"], budget)
            self.assertEqual(request.call_count, budget - 1)
            self.assertEqual(bool(p["abstract"]), budget == 2)
        p = paper(identifier="conf/icml/Mismatch", ee="")
        result = self.enrich([p], 1, mock.Mock(return_value={"doi": "10.1234/wrong", "abstract_inverted_index": {"Wrong": [0]}}))
        self.assertEqual(result["failed"], 1)
        self.assertFalse(p["abstract"])

    def test_cached_citation_is_repaired_without_resetting_delivery(self):
        p = paper(abstract=CITATION)
        self.store.save_discovery_page("icml", 2025, [p], {})
        self.store.save_metadata(p, "success")
        run = self.store.start_run("test")
        self.store.record_selection(run, [p]); self.store.finish_delivery(run, True)
        self.assertFalse(self.store.metadata_due(p["id"]))
        result = self.enrich([p], 1)
        self.assertEqual(result["invalidated"], 1)
        self.assertEqual(p["abstract"], ABSTRACT)
        self.assertEqual(self.store.delivered_ids(), {p["id"]})

    def test_zero_budget_still_removes_bad_cached_abstract(self):
        p = paper(abstract=CITATION)
        result = self.enrich([p], 0)
        self.assertEqual(result["requests"], 0)
        self.assertFalse(p["abstract"])
        self.assertFalse(self.store.candidates(["icml"], [2025])[0]["abstract"])

    def test_openalex_citation_and_credentials_are_not_saved(self):
        p = paper(ee="")
        response = {"doi": "10.1234/test", "abstract_inverted_index": {CITATION: [0]}}
        with mock.patch.dict("os.environ", {"OPENALEX_API_KEY": "synthetic-secret"}):
            result = self.enrich([p], 1, mock.Mock(return_value=response))
        self.assertEqual(result["failed"], 1)
        self.assertNotIn("synthetic-secret", json.dumps(p))
        self.assertFalse(p["abstract"])

    def test_rate_limit_does_not_block_other_official_hosts(self):
        p = paper(doi="")
        q = paper("conf/nips/Test25", ee="https://proceedings.neurips.cc/paper_files/paper/2025/hash/abcd-Abstract-Conference.html", doi="")
        fetch = mock.Mock(side_effect=[urllib.error.HTTPError(p["ee"], 429, "rate limit", {}, None), HTML])
        result = self.enrich([p, q], 2, fetch=fetch)
        self.assertEqual((result["requests"], result["enriched"]), (2, 1))
        self.assertEqual(q["abstract"], ABSTRACT)

    def test_acl_doi_maps_only_to_matching_main_year(self):
        p = paper(ee="https://doi.org/10.18653/V1/2025.ACL-LONG.17")
        self.assertEqual(official_abstracts.official_url(p), "https://aclanthology.org/2025.acl-long.17/")
        self.assertIsNone(official_abstracts.official_url({**p, "year": 2026}))

    def test_actions_profile_is_explicit_and_keeps_shadow(self):
        env = {"SMTP_SENDER": "sender@example.org", "SMTP_RECEIVER": "receiver@example.org",
               "LLM_BASE_URL": "https://example.org/v1", "LLM_MODEL": "test", "SEMANTIC_MODE": "shadow",
               "RELATION_MODE": "shadow", "RESEARCH_PROFILE": "rl-action-world-models"}
        output = self.root / "action.json"
        with mock.patch.dict("os.environ", env, clear=True):
            build(ROOT / "config.example.json", output, self.root, 10, False)
        config = json.loads(output.read_text(encoding="utf-8"))
        self.assertIn("imitation learning", config["explicit_interests"])
        self.assertFalse(config["delivery"]["smtp"]["enabled"])
        self.assertEqual(config["semantic"]["mode"], "shadow")
        self.assertEqual(config["relation_policy"]["mode"], "shadow")
        with mock.patch.dict("os.environ", {**env, "RESEARCH_PROFILE": "../../private"}, clear=True):
            with self.assertRaises(ValueError):
                build(ROOT / "config.example.json", output, self.root, 10)

    def test_full_preview_uses_official_abstract_without_touching_original_state(self):
        p = paper(doi="", venue="ICML", rank="A", type="conference",
                  url="https://dblp.org/rec/conf/icml/Test25", proceedings_id="https://dblp.org/rec/conf/icml/2025")
        config = {"years": [2025], "venue_keys": ["icml"], "state_db": str(self.store.path),
                  "seen_db": str(self.root / "seen.json"), "explicit_interests": ["world model"],
                  "topic_priority": {"primary_topics": ["world model"]}, "dblp": {"max_pages_per_run": 1},
                  "metadata": {"max_requests_per_run": 1}, "delivery": {"smtp": {"enabled": True}}}
        (self.root / "config.json").write_text(json.dumps(config), encoding="utf-8")
        (self.root / "seen.json").write_text(json.dumps({"ids": ["conf/icml/PreviouslyDelivered"]}), encoding="utf-8")
        original_seen = (self.root / "seen.json").read_bytes()
        (self.root / "interests.json").write_text(json.dumps([{"id": "ref", "title": "Control policy learning"}]), encoding="utf-8")
        venues = {"venues": [{"dblp_key": "icml", "abbr": "ICML", "name": "ICML", "rank": "A", "type": "conference"}]}
        (self.root / "venues.json").write_text(json.dumps(venues), encoding="utf-8")
        argv = ["paper_scout", "--config", str(self.root / "config.json"), "--interests", str(self.root / "interests.json"),
                "--venues", str(self.root / "venues.json"), "--output", str(self.root / "preview.md"), "--no-update-seen"]
        with mock.patch("sys.argv", argv), mock.patch.object(paper_scout, "fetch_dblp_page", return_value=([p], 1, 1)), \
                mock.patch.object(official_abstracts, "fetch", return_value=HTML) as fetch, \
                mock.patch.object(paper_scout, "send_email") as send, mock.patch("socket.socket.connect", side_effect=AssertionError("network")):
            self.assertEqual(paper_scout.main(), 0)
        fetch.assert_called_once()
        send.assert_not_called()
        diagnostic = json.loads((self.root / "preview.diagnostics.json").read_text(encoding="utf-8"))
        self.assertEqual(diagnostic["selected_ids"], [p["id"]])
        self.assertEqual((self.root / "seen.json").read_bytes(), original_seen)
        self.assertEqual(self.store.candidates(["icml"], [2025]), [])
        self.assertEqual(self.store.delivered_ids(), set())


if __name__ == "__main__":
    unittest.main()
