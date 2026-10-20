import argparse
import datetime as dt
import json
import pathlib
import sqlite3
import tempfile
import unittest
import urllib.error
from unittest import mock

import paper_scout as scout
import state_store
from ccf_paper_scout import discovery, metadata, selection, semantic
from ccf_paper_scout.cli import load_config


def paper(index=0, **overrides):
    return {"id": f"conf/aaai/Test{index}", "title": "Multi-agent reinforcement learning", "abstract": "Policies learn cooperative reinforcement learning with rewards.",
            "authors": ["Test Author"], "year": 2026, "venue": "AAAI", "venue_name": "AAAI", "rank": "A", "type": "conference",
            "url": f"https://dblp.org/rec/conf/aaai/Test{index}", "doi": "10.1234/test", "ee": "", "lexical_score": 0.3,
            "score": 0.3, "reasons": ["reinforcement"], "topic_class": "primary", **overrides}


VENUES = {"aaai": {"dblp_key": "aaai", "abbr": "AAAI", "rank": "A", "type": "conference", "name": "AAAI"}}


class StoreFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.store = state_store.StateStore(self.root / "state.sqlite3")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()


class StoreTests(StoreFixture, unittest.TestCase):
    def test_original_schema_is_upgraded(self):
        path = self.root / "old.sqlite3"
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE source_cursors(source TEXT,venue_key TEXT,year INTEGER,next_offset INTEGER,last_checked_at TEXT,PRIMARY KEY(source,venue_key,year))")
        connection.execute("INSERT INTO source_cursors VALUES ('DBLP','aaai',2026,200,'2026-10-01T00:00:00+00:00')")
        connection.commit()
        connection.close()
        upgraded = state_store.StateStore(path)
        try:
            self.assertEqual(upgraded.load_cursor("DBLP", "aaai", 2026), 200)
            upgraded.save_cursor("DBLP", "aaai", 2026, 300)
            self.assertEqual(upgraded.scan_state("aaai", 2026)["next_offset"], 300)
        finally:
            upgraded.close()


    def test_page_and_cursor_commit_is_atomic(self):
        with self.assertRaises(KeyError):
            self.store.save_discovery_page("aaai", 2026, [paper(), {"title": "invalid"}], {"next_offset": 2})
        self.assertEqual(self.store.candidates(["aaai"], [2026]), [])
        self.assertEqual(self.store.load_cursor("DBLP", "aaai", 2026), 0)

    def test_refresh_keeps_abstract_and_first_discovery(self):
        self.store.save_discovery_page("aaai", 2026, [paper()], {})
        before = self.store.candidates(["aaai"], [2026])[0]
        self.store.save_discovery_page("aaai", 2026, [paper(abstract="")], {"initialized": True})
        after = self.store.candidates(["aaai"], [2026])[0]
        self.assertEqual(before["first_seen_at"], after["first_seen_at"])
        self.assertTrue(after["abstract"])
        self.assertTrue(after["historical_import"])

    def test_old_database_migrates_without_losing_delivered(self):
        run = self.store.start_run("test")
        self.store.record_selection(run, [paper()])
        self.store.finish_delivery(run, True)
        self.store.close()
        self.store = state_store.StateStore(self.root / "state.sqlite3")
        self.assertEqual(self.store.delivered_ids(), {paper()["id"]})
        self.store.save_cursor("DBLP", "aaai", 2026, 100)
        self.assertEqual(self.store.load_cursor("DBLP", "aaai", 2026), 100)

    def test_vector_invalidates_on_text_and_model_change(self):
        self.store.save_vector("p", "hash", "model", 1, b"1234")
        self.assertEqual(self.store.load_vector("p", "hash", "model"), b"1234")
        self.assertIsNone(self.store.load_vector("p", "other", "model"))
        self.assertIsNone(self.store.load_vector("p", "hash", "new-model"))

    def test_preview_snapshot_is_read_only_and_smtp_disabled(self):
        self.store.save_discovery_page("aaai", 2026, [paper()], {})
        config = {"state_db": str(self.store.path), "delivery": {"smtp": {"enabled": True}}}
        target = self.root / "preview"
        target.mkdir()
        copied = scout.isolated_config(config, self.root, target)
        snapshot = state_store.StateStore(pathlib.Path(copied["state_db"]))
        try:
            snapshot.save_discovery_page("aaai", 2026, [paper(1)], {})
        finally:
            snapshot.close()
        self.assertEqual(len(self.store.candidates(["aaai"], [2026])), 1)
        self.assertFalse(copied["delivery"]["smtp"]["enabled"])
        self.assertTrue(config["delivery"]["smtp"]["enabled"])


class DiscoveryTests(StoreFixture, unittest.TestCase):
    def config(self, **overrides):
        return {"page_size": 2, "max_pages_per_run": 1, "max_pages_per_partition_per_run": 1, **overrides}

    def test_unrelated_unseen_records_do_not_stop_next_run(self):
        offsets = []

        def fetch(venue, year, limit, offset, agent, config):
            offsets.append(offset)
            return [paper(offset), paper(offset + 1)], 6, 2

        discovery.discover(self.store, VENUES, [2026], self.config(), "test", fetch)
        discovery.discover(self.store, VENUES, [2026], self.config(), "test", fetch)
        self.assertEqual(offsets, [0, 2])
        self.assertEqual(len(self.store.candidates(["aaai"], [2026])), 4)
        self.assertEqual(self.store.scan_state("aaai", 2026)["status"], "partial")

    def test_failed_page_does_not_advance(self):
        self.store.save_discovery_page("aaai", 2026, [], {"next_offset": 0})
        with self.assertRaisesRegex(RuntimeError, "success ratio"):
            discovery.discover(self.store, VENUES, [2026], self.config(), "test", mock.Mock(side_effect=RuntimeError("timeout")))
        self.assertEqual(self.store.load_cursor("DBLP", "aaai", 2026), 0)

    def test_overlap_has_forward_progress(self):
        fetch = mock.Mock(return_value=([paper(), paper(1)], None, 2))
        discovery.discover(self.store, VENUES, [2026], self.config(), "test", fetch)
        discovery.discover(self.store, VENUES, [2026], self.config(max_pages_per_run=2, max_pages_per_partition_per_run=2), "test", fetch)
        self.assertEqual([call.args[3] for call in fetch.call_args_list], [0, 0, 2])
        self.assertEqual(self.store.load_cursor("DBLP", "aaai", 2026), 4)

    def test_transport_change_resets_offset(self):
        fetch = mock.Mock(return_value=([paper(), paper(1)], None, 2))
        discovery.discover(self.store, VENUES, [2026], self.config(transport="search"), "test", fetch)
        discovery.discover(self.store, VENUES, [2026], self.config(transport="sparql"), "test", fetch)
        self.assertEqual([call.args[3] for call in fetch.call_args_list], [0, 0])

    def test_empty_probe_is_not_source_failure(self):
        fetch = mock.Mock(side_effect=lambda venue, year, *args: ([], 0, 0))
        stats, failures = discovery.discover(self.store, VENUES, [2026], self.config(max_pages_per_run=2), "test", fetch, probe_year=2027)
        self.assertEqual(stats["success_ratio"], 1.0)
        self.assertFalse(failures)
        self.assertEqual(self.store.scan_state("aaai", 2027)["status"], "not_indexed_yet")

    def test_bootstrap_and_later_discoveries_are_distinguished(self):
        discovery.discover(self.store, VENUES, [2026], self.config(), "test", mock.Mock(return_value=([paper()], 1, 1)))
        state = self.store.scan_state("aaai", 2026)
        state["next_check_at"] = ""
        self.store.save_discovery_page("aaai", 2026, [], state)
        discovery.discover(self.store, VENUES, [2026], self.config(), "test", mock.Mock(return_value=([paper(1)], 1, 1)))
        saved = {p["id"]: p for p in self.store.candidates(["aaai"], [2026])}
        self.assertTrue(saved[paper()["id"]]["historical_import"])
        self.assertFalse(saved[paper(1)["id"]]["historical_import"])


class YearAndSelectionTests(unittest.TestCase):
    def test_cli_accepts_rolling_config_without_years(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "config.json"
            path.write_text(json.dumps({"year_policy": {"mode": "rolling"}, "venue_keys": ["aaai"]}), encoding="utf-8")
            self.assertEqual(load_config(path)["venue_keys"], ["aaai"])

    def test_year_rollover_and_fixed_mode(self):
        config = {"year_policy": {"mode": "rolling", "lookback_years": 1}}
        self.assertEqual(discovery.resolve_years(config, dt.date(2026, 12, 31)), [2026, 2025])
        self.assertEqual(discovery.resolve_years(config, dt.date(2027, 1, 1)), [2027, 2026])
        self.assertEqual(discovery.resolve_years({"years": [2025]}), [2025])
        with self.assertRaises(ValueError):
            discovery.resolve_years({"years": [True]})

    def test_three_qualified_are_not_filled_to_ten(self):
        candidates = [paper(i) for i in range(3)] + [paper(4, topic_class="other", lexical_score=10)]
        self.assertEqual(len(selection.select(candidates, 10, {})), 3)
        self.assertEqual(len(scout.select_topic_balanced(candidates, 10, {})), 3)

    def test_zero_qualified_and_missing_abstract(self):
        self.assertEqual(selection.select([paper(abstract="")], 10, {}), [])
        self.assertEqual(selection.select([paper(topic_class="other")], 10, {}), [])

    def test_exploration_cap_and_new_discovery(self):
        current = dt.datetime.now(dt.timezone.utc).isoformat()
        candidates = [paper(0, lexical_score=0.5), paper(1, historical_import=False, first_seen_at=current),
                      paper(2, topic_class="exploration"), paper(3, topic_class="exploration")]
        selected = selection.select(candidates, 10, {})
        self.assertEqual(selected[0]["id"], paper(1)["id"])
        self.assertEqual(sum(p["topic_class"] == "exploration" for p in selected), 1)

    def test_lexical_repetition_is_not_unbounded(self):
        candidates = [paper(abstract="reinforcement " * size) for size in (10, 1000)]
        ranked = scout.rank_candidates([paper()], candidates, ["reinforcement learning"])
        self.assertLess(max(p["lexical_score"] for p in ranked), 3)

    def test_unlabeled_evaluation_is_not_assumed_negative(self):
        metrics = selection.metrics([paper(), paper(1)], {paper()["id"]: True})
        self.assertEqual(metrics["precision"], 1)
        self.assertEqual(metrics["unlabeled_selected"], 1)


class MetadataTests(StoreFixture, unittest.TestCase):
    def test_request_budget_and_negative_cache(self):
        papers = [paper(i, abstract="") for i in range(3)]
        self.store.save_discovery_page("aaai", 2026, papers, {})
        request = mock.Mock(return_value={"abstract_inverted_index": None})
        config = {"max_requests_per_run": 1}
        metadata.enrich(papers, self.store, config, "test", request, scout.normalize_doi, scout.openalex_abstract)
        self.assertEqual(request.call_count, 1)
        self.assertFalse(self.store.metadata_due(papers[0]["id"]))
        metadata.enrich(papers, self.store, config, "test", request, scout.normalize_doi, scout.openalex_abstract)
        self.assertEqual(request.call_count, 2)

    def test_doi_field_without_ee_and_rate_limit(self):
        papers = [paper(i, abstract="") for i in range(3)]
        request = mock.Mock(side_effect=urllib.error.HTTPError("https://example.org", 429, "rate limited", {}, None))
        stats = metadata.enrich(papers, self.store, {"max_requests_per_run": 3}, "test", request, scout.normalize_doi, scout.openalex_abstract)
        self.assertEqual(stats["requests"], 1)
        self.assertIn("10.1234", request.call_args.args[0])


class FakeEncoder:
    signature = "fixed-test-encoder"

    def encode_query(self, text):
        return [1.0, 0.0]

    def encode_papers(self, papers):
        return [[1.0, 0.0] if "reinforcement" in p["title"].lower() else [0.0, 1.0] for p in papers]


class SemanticTests(StoreFixture, unittest.TestCase):
    def test_incremental_caching_and_text_update(self):
        config = {"explicit_interests": ["RL"], "semantic": {"max_updated_papers_per_run": 1}}
        papers = [paper(), paper(1)]
        first = semantic.rank(papers, [], config, self.store, encoder=FakeEncoder())
        self.assertEqual((first["encoded"], first["deferred"]), (1, 1))
        second = semantic.rank(papers, [], config, self.store, encoder=FakeEncoder())
        self.assertEqual((second["cache_hits"], second["encoded"]), (1, 1))
        self.assertNotIn("semantic_deferred", papers[1])
        papers[0]["abstract"] += " Updated"
        third = semantic.rank(papers, [], config, self.store, encoder=FakeEncoder())
        self.assertEqual(third["encoded"], 1)

    def test_calibration_required_and_profile_bound(self):
        stats = {"model_signature": "model", "profile_hash": "profile"}
        with self.assertRaisesRegex(ValueError, "calibration"):
            semantic.calibrated_thresholds({}, stats, preview=False)
        self.assertIsNone(semantic.calibrated_thresholds({}, stats, preview=True))
        calibration = {**stats, "approved": True, "thresholds": {"abstract": 0.5, "title_only": 0.7}}
        config = {"selection": {"calibration": calibration}}
        self.assertEqual(semantic.calibrated_thresholds(config, stats, preview=False)["abstract"], 0.5)
        with self.assertRaises(ValueError):
            semantic.calibrated_thresholds(config, {**stats, "profile_hash": "changed"}, preview=False)

    def test_review_requires_exact_abstract_evidence(self):
        config = {"llm_translation": {"base_url": "https://example.org/v1", "model": "test"}}
        response = {"choices": [{"message": {"content": json.dumps({"decision": "relevant", "direction": "RL", "evidence": "invented text", "reason": "test"})}}]}
        papers = [paper()]
        with mock.patch.dict("os.environ", {"LLM_API_KEY": "test"}):
            selection.review(papers, config, self.store, "test", mock.Mock(return_value=response), json.loads)
        self.assertEqual(papers[0]["relevance_review"]["decision"], "uncertain")


class PipelineTests(StoreFixture, unittest.TestCase):
    def test_full_cli_preview_keeps_production_cursor_and_delivered(self):
        self.store.save_discovery_page("aaai", 2026, [paper()], {"next_offset": 200})
        configuration = {"years": [2026], "venue_keys": ["aaai"], "state_db": str(self.store.path),
                         "explicit_interests": ["reinforcement learning"], "topic_priority": {"primary_topics": ["reinforcement learning"]},
                         "dblp": {"max_pages_per_run": 1}, "metadata": {"max_requests_per_run": 0},
                         "delivery": {"smtp": {"enabled": True}}}
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps(configuration), encoding="utf-8")
        interests_path = self.root / "interests.json"
        interests_path.write_text(json.dumps([paper(9, title="Reinforcement learning reference")]), encoding="utf-8")
        venues_path = self.root / "venues.json"
        venues_path.write_text(json.dumps({"venues": list(VENUES.values())}), encoding="utf-8")
        argv = ["paper_scout", "--config", str(config_path), "--interests", str(interests_path), "--venues", str(venues_path),
                "--output", str(self.root / "preview.md"), "--no-update-seen"]
        with mock.patch("sys.argv", argv), mock.patch.object(scout, "fetch_dblp_page", return_value=([], 0, 0)), mock.patch.object(scout, "send_email") as send:
            self.assertEqual(scout.main(), 0)
        send.assert_not_called()
        self.assertEqual(self.store.load_cursor("DBLP", "aaai", 2026), 200)
        self.assertEqual(self.store.delivered_ids(), set())

    def test_zero_selection_saves_discovery_and_never_sends(self):
        config = {"years": [2026], "venue_keys": ["aaai"], "explicit_interests": ["reinforcement learning"],
                  "dblp": {"page_size": 2, "max_pages_per_run": 1}, "metadata": {"max_requests_per_run": 0},
                  "seen_db": str(self.root / "seen.json"), "state_db": str(self.store.path), "delivery": {"smtp": {"enabled": True}}}
        args = argparse.Namespace(config=self.root / "config.json", output=self.root / "recommendations.md", labels=None,
                                  no_update_seen=False, discover_only=False, evaluate_only=False)
        run = self.store.start_run("test")
        unrelated = paper(title="Pottery classification", abstract="Historical ceramic trade.")
        with mock.patch.object(scout, "fetch_dblp_page", return_value=([unrelated], 1, 1)), mock.patch.object(scout, "send_email") as send:
            scout.run_pipeline_body(args, config, "test", [paper()], [], self.store, run, self.root / "seen.json", set(), ["aaai"], VENUES)
        send.assert_not_called()
        self.assertEqual(len(self.store.candidates(["aaai"], [2026])), 1)
        self.assertEqual(self.store.connection.execute("SELECT status FROM recommendation_runs WHERE run_id=?", (run,)).fetchone()[0], "no_qualified_papers")

    def test_snapshot_evaluation_never_queries_sources(self):
        self.store.save_discovery_page("aaai", 2026, [paper()], {})
        config = {"years": [2026], "explicit_interests": ["reinforcement learning"],
                  "topic_priority": {"primary_topics": ["reinforcement learning"]}, "metadata": {"max_requests_per_run": 0}}
        args = argparse.Namespace(config=self.root / "config.json", output=self.root / "eval.md", labels=None,
                                  no_update_seen=True, discover_only=False, evaluate_only=True)
        run = self.store.start_run("test")
        with mock.patch.object(scout, "fetch_dblp_page") as fetch, mock.patch.object(scout, "send_email") as send:
            scout.run_pipeline_body(args, config, "test", [paper()], [], self.store, run, self.root / "seen.json", set(), ["aaai"], VENUES)
        fetch.assert_not_called()
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
