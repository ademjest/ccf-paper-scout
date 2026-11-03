import importlib.util
import json
import pathlib
import tempfile
import unittest
import urllib.error
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("research_experiment", ROOT / "scripts/research_experiment.py")
research = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(research)

ABSTRACT = "An agent learns long-horizon control policies by planning in an action-conditioned latent world model."
HTML = '<meta name="citation_title" content="World Model"><meta name="citation_author" content="Ada Smith"><div id="abstract">' + ABSTRACT + '</div>'


def paper(identifier, family="world_models", **values):
    return {"id": identifier, "title": "World Model", "authors": ["Ada Smith"], "year": 2025,
            "ee": "https://proceedings.mlr.press/v267/test25.html", "query_families": [family], **values}


class ResearchExperimentTests(unittest.TestCase):
    def test_queries_are_bounded_and_escape_regex_literals(self):
        query = research.query_text("icml", 'world "model"')
        self.assertIn('FILTER(?year = "2025"^^xsd:gYear)', query)
        self.assertIn('LIMIT 50', query)
        self.assertIn('world \\"model\\"', query)
        with self.assertRaises(ValueError):
            research.query_text('icml> . ?x ?y ?z', 'world')

    def test_profile_overlay_never_enables_delivery_or_role_enforcement(self):
        example = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        profile = json.loads((ROOT / "profiles/rl-action-world-models.json").read_text(encoding="utf-8"))
        profile["delivery"] = {"smtp": {"enabled": True}}
        config = research.research_config(example, profile)
        self.assertFalse(config["delivery"]["smtp"]["enabled"])
        self.assertFalse(config["relation_policy"]["review"]["enabled"])
        self.assertEqual(config["semantic"]["mode"], "shadow")
        self.assertEqual(config["relation_policy"]["mode"], "shadow")
        self.assertNotIn("zotero", config)
        self.assertIn("imitation learning", config["explicit_interests"])

    def test_shared_candidates_are_only_acquired_once(self):
        a = paper("conf/icml/A", query_families=["world_models", "action_policies"])
        b = paper("conf/nips/B", "self_play")
        self.assertEqual({p["id"] for p in research.acquisition_order([a, b])}, {a["id"], b["id"]})
        self.assertEqual(len(research.acquisition_order([a, b])), 2)

    def test_budget_and_wrong_paper_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = [paper("conf/icml/A"), paper("conf/icml/B")]
            fetch = mock.Mock(return_value=HTML.replace('content="World Model"', 'content="Other Paper"'))
            attempts = research.enrich(rows, pathlib.Path(directory), 1, fetch, delay=0)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(attempts[0]["status"], "failed")
            self.assertTrue(all(not p["abstract"] for p in rows))
            self.assertEqual(rows[1]["metadata_status"], "budget_not_covered")

    def test_rate_limited_source_stops_without_blocking_other_hosts(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = [paper("conf/icml/A"), paper("conf/icml/B"),
                    paper("conf/nips/C", ee="https://proceedings.neurips.cc/paper_files/paper/2025/hash/abcd-Abstract-Conference.html")]
            def fetch(url):
                if "mlr.press" in url:
                    raise urllib.error.HTTPError(url, 429, "rate limited", {}, None)
                return HTML
            attempts = research.enrich(rows, pathlib.Path(directory), 3, fetch, delay=0)
            self.assertEqual(len(attempts), 2)
            self.assertEqual(rows[1]["metadata_status"], "source_throttled")
            self.assertEqual(rows[2]["abstract"], ABSTRACT)

    def test_incomplete_discovery_is_reported_and_does_not_proceed(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = {"results": {"bindings": []}}
            with mock.patch.object(research.paper_scout, "request_dblp_json", side_effect=[RuntimeError("offline")] + [payload] * 11):
                with self.assertRaisesRegex(RuntimeError, "discovery incomplete"):
                    research.discover(pathlib.Path(directory))
            audit = json.loads((pathlib.Path(directory) / "discovery.json").read_text(encoding="utf-8"))
            self.assertEqual(len(audit["queries"]), 12)
            self.assertEqual(audit["queries"][0]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
