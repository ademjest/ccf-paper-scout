import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import paper_scout as scout
from ccf_paper_scout import proceedings, relations, selection, semantic
from state_store import StateStore
from test_recommendation_upgrade import FakeEncoder, paper


def acl(**overrides):
    return paper(id="conf/acl/example", venue="ACL", year=2025,
                 proceedings_id="https://dblp.org/rec/conf/acl/2025-1", **overrides)


def role_review(labels=None, marl=False):
    quote = "We optimize language model policies with reinforcement learning."
    return {"status": "classified", "labels": labels or ["rl_for_llm"], "substantial_marl": marl,
            **{field: {"answer": "Role described in abstract", "quote": quote if field != "marl" or marl else ""}
               for field in ("optimized_model", "llm_role", "rl_role", "marl")}}


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = StateStore(Path(self.temp.name) / "test.sqlite3")
        self.addCleanup(self.store.close)
        self.catalog = proceedings.registry()

    def test_main_long_short_and_poster(self):
        for suffix in ("1", "2"):
            p = acl()
            p["proceedings_id"] = "https://dblp.org/rec/conf/acl/2025-" + suffix
            p["presentation"] = "poster"
            self.assertEqual(proceedings.qualify(p, self.catalog), "verified")

    def test_findings_tutorial_demo_and_student_are_excluded(self):
        for suffix in ("f", "-3", "-4", "-5"):
            p = acl()
            p["proceedings_id"] = "https://dblp.org/rec/conf/acl/2025" + suffix
            self.assertEqual(proceedings.qualify(p, self.catalog), "excluded")

    def test_unknown_industry_and_wrong_year_fail_closed(self):
        for identifier in ("2025-6", "2025unknown", "2026-1"):
            p = acl()
            p["proceedings_id"] = "https://dblp.org/rec/conf/acl/" + identifier
            self.assertEqual(proceedings.qualify(p, self.catalog), "unresolved")

    def test_aaai_official_issue_not_article_suffix(self):
        p = paper(year=2025, proceedings_id="https://dblp.org/rec/conf/aaai/2025", doi="10.1609/AAAI.V39I20.35421")
        self.assertEqual(proceedings.qualify(p, self.catalog), "verified")
        self.assertIn("aaai-39-2025", p["verification_source"])
        for doi in ("10.1609/aaai.v39i28.35421", "10.1609/aaai.v40i20.35421", "10.9999/aaai.v39i20.35421", ""):
            p["doi"] = doi
            self.assertEqual(proceedings.qualify(p, self.catalog), "unresolved")

    def test_mixed_sigir_is_not_blanket_approved(self):
        p = paper(id="conf/sigir/example", year=2025, proceedings_id="https://dblp.org/rec/conf/sigir/2025")
        self.assertEqual(proceedings.qualify(p, self.catalog), "unresolved")

    def test_neurips_requires_official_main_track_document_marker(self):
        p = paper(id="conf/nips/example", year=2025, proceedings_id="https://dblp.org/rec/conf/nips/2025",
                  ee="https://papers.nips.cc/paper_files/paper/2025/hash/0010031a1b4910aa67edbda26a705518-Abstract-Conference.html")
        self.assertEqual(proceedings.qualify(p, self.catalog), "verified")
        p["ee"] = p["ee"].replace("-Conference", "-Creative_AI_Track")
        self.assertEqual(proceedings.qualify(p, self.catalog), "unresolved")
        p["ee"] = "https://example.org/paper-Abstract-Conference.html"
        self.assertEqual(proceedings.qualify(p, self.catalog), "unresolved")

    def test_tutorial_abstract_overrides_volume(self):
        p = acl(abstract="In this tutorial we discuss reinforcement learning for language models.")
        self.assertEqual(proceedings.qualify(p, self.catalog), "excluded")

    def test_historical_recheck_keeps_delivery_and_first_seen(self):
        p = acl()
        self.store.save_discovery_page("acl", 2025, [p], {})
        run = self.store.start_run("test")
        self.store.record_selection(run, [p])
        self.store.finish_delivery(run, True)
        old = self.store.candidates(["acl"], [2025])[0]
        old["proceedings_id"] = "unknown"
        eligible, stats = proceedings.gate([old], self.store)
        self.assertEqual(eligible, [])
        self.assertEqual(stats, {"unresolved": 1})
        saved = self.store.candidates(["acl"], [2025])[0]
        self.assertEqual(saved["first_seen_at"], old["first_seen_at"])
        self.assertEqual(saved["main_conference_status"], "unresolved")
        self.assertIn(p["id"], self.store.delivered_ids())

    def test_bounded_old_candidate_backfill_and_negative_cache(self):
        papers = [paper(i, proceedings_id="") for i in range(3)]
        self.store.save_discovery_page("aaai", 2026, papers, {})
        lookup = mock.Mock(return_value={})
        result = proceedings.backfill(papers, self.store, {"max_backfill_papers_per_run": 2}, lookup)
        self.assertEqual((result["checked"], result["remaining"]), (2, 1))
        proceedings.backfill(papers, self.store, {}, lookup)
        self.assertEqual(lookup.call_args.args[0], [papers[2]["id"]])

    def test_lookup_failure_leaves_candidates_retryable(self):
        p = paper(proceedings_id="")
        result = proceedings.backfill([p], self.store, {}, mock.Mock(side_effect=RuntimeError("unavailable")))
        self.assertEqual(result["failed_batches"], 1)
        self.assertNotIn("proceedings_checked_at", p)

    def test_pure_rl_for_llm_cannot_bypass_general_rl(self):
        p = paper(relation_decision="exclude")
        cfg = {"relation_policy": {"enabled": True, "mode": "production"}}
        self.assertEqual(selection.select([p], 10, cfg), [])
        self.assertEqual(p["selection_reason"], "relation_exclude")
        cfg["relation_policy"]["mode"] = "shadow"
        self.assertEqual(len(selection.select([p], 10, cfg)), 1)

    def test_unverified_cannot_bypass_semantic_or_review(self):
        p = paper(main_conference_status="unresolved", semantic_score=1, relation_decision="allow",
                  relevance_review={"decision": "relevant"})
        self.assertEqual(selection.select([p], 10, {}, {"abstract": 0, "title_only": 0}, semantic_production=True), [])

    def test_general_rl_without_language_model_stays_eligible(self):
        p = paper()
        result = relations.initial(p, {})
        self.assertEqual(result["status"], "not_applicable")
        self.assertEqual(relations.decision(result), "allow")

    def test_no_abstract_or_ambiguous_language_model_defers(self):
        for p in (paper(abstract=""), paper(abstract="We combine LLMs and reinforcement learning.")):
            self.assertEqual(relations.decision(relations.initial(p, {})), "defer")

    def test_exact_role_evidence_required(self):
        value = role_review()
        abstract = value["llm_role"]["quote"]
        self.assertEqual(relations.decision(relations.validate_review(value, abstract)), "exclude")
        value["optimized_model"]["quote"] = "invented evidence"
        with self.assertRaises(ValueError):
            relations.validate_review(value, abstract)

    def test_mixed_roles_and_substantial_marl_are_preserved(self):
        for labels, marl in ((["rl_for_llm", "llm_for_rl"], False), (["rl_for_llm", "llm_multiagent_rl"], True)):
            value = role_review(labels, marl)
            result = relations.validate_review(value, value["llm_role"]["quote"])
            self.assertEqual(set(result["labels"]), set(labels))
            self.assertEqual(relations.decision(result), "allow")

    def test_multiple_agents_without_marl_evidence_is_invalid(self):
        value = role_review(["llm_multiagent_rl"])
        with self.assertRaises(ValueError):
            relations.validate_review(value, value["llm_role"]["quote"])

    def test_background_only_llm_mention_can_keep_traditional_rl(self):
        value = role_review()
        value.update(status="not_applicable", labels=[])
        result = relations.validate_review(value, value["llm_role"]["quote"])
        self.assertEqual(relations.decision(result), "allow")

    def test_marl_boundary_is_not_excluded_by_single_prototype(self):
        p = paper(abstract="Language model agents learn joint policies with reinforcement learning.", relation_signature="s",
                  relation_scores={"llm_for_rl": 0.2, "rl_for_llm": 0.8, "llm_multiagent_rl": 0.3}, relation_margin=-0.6)
        config = {"calibration": {"approved": True, "relation_signature": "s", "thresholds": {"positive": 0.6, "margin": 0.1}}}
        self.assertEqual(relations.initial(p, config)["status"], "uncertain")

    def test_uncertain_review_is_temporarily_cached_to_avoid_starvation(self):
        papers = [paper(abstract="We study reinforcement learning for language models.")]
        config = {"llm_translation": {"base_url": "https://example.org/v1", "model": "test"}}
        response = {"choices": [{"message": {"content": '{"status":"uncertain"}'}}]}
        post = mock.Mock(return_value=response)
        with mock.patch.dict("os.environ", {"LLM_API_KEY": "test"}):
            relations.assess(papers, config, self.store, "test", post, json.loads)
            stats = relations.assess(papers, config, self.store, "test", post, json.loads)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(stats["review_cache_hits"], 1)
        self.assertEqual(papers[0]["relation_decision"], "defer")

    def test_uncalibrated_small_margin_never_forces_label(self):
        p = paper(abstract="We study LLM reinforcement learning.", relation_signature="s",
                  relation_scores={"llm_for_rl": 0.71, "rl_for_llm": 0.70, "llm_multiagent_rl": 0.2}, relation_margin=0.01)
        self.assertEqual(relations.initial(p, {})["status"], "uncertain")
        cfg = {"calibration": {"approved": True, "relation_signature": "s", "thresholds": {"positive": 0.6, "margin": 0.1}}}
        self.assertEqual(relations.initial(p, cfg)["status"], "uncertain")
        cfg["calibration"]["approved"] = "true"
        with self.assertRaises(ValueError):
            relations.initial(p, cfg)

    def test_directional_calibration_and_version_binding(self):
        p = paper(abstract="Language models improve exploration.", relation_signature="s",
                  relation_scores={"llm_for_rl": 0.8, "rl_for_llm": 0.3, "llm_multiagent_rl": 0.2}, relation_margin=0.5)
        cfg = {"calibration": {"approved": True, "relation_signature": "s", "thresholds": {"positive": 0.6, "margin": 0.1}}}
        self.assertEqual(relations.initial(p, cfg)["labels"], ["llm_for_rl"])
        cfg["calibration"]["relation_signature"] = "stale"
        with self.assertRaises(ValueError):
            relations.initial(p, cfg)

    def test_prototypes_reuse_paper_vectors_and_cache(self):
        cfg = {"explicit_interests": ["RL"], "relation_policy": {"enabled": True}}
        papers = [paper()]
        first = semantic.rank(papers, [], cfg, self.store, encoder=FakeEncoder())
        second = semantic.rank(papers, [], cfg, self.store, encoder=FakeEncoder())
        self.assertEqual(first["encoded"], 1)
        self.assertEqual(second["encoded"], 0)
        self.assertEqual(second["relations"]["prototype_cache_hits"], 9)
        self.assertEqual(set(papers[0]["relation_scores"]), set(relations.PROTOTYPES))
        original = relations.signature(FakeEncoder.signature)
        with mock.patch.dict(relations.PROTOTYPES, {"llm_for_rl": ["Changed prototype"]}):
            self.assertNotEqual(original, relations.signature(FakeEncoder.signature))

    def test_deferred_vector_clears_old_relation_scores(self):
        cfg = {"explicit_interests": ["RL"], "relation_policy": {"enabled": True}, "semantic": {"max_updated_papers_per_run": 0}}
        papers = [paper(relation_scores={"rl_for_llm": 1}, relation_signature="stale")]
        semantic.rank(papers, [], cfg, self.store, encoder=FakeEncoder())
        self.assertNotIn("relation_scores", papers[0])

    def test_boundary_review_budget_and_cache(self):
        value = role_review()
        papers = [paper(i, abstract=value["llm_role"]["quote"] + f" Case {i}.") for i in range(3)]
        config = {"relation_policy": {"mode": "production", "review": {"max_candidates_per_run": 1}},
                  "llm_translation": {"base_url": "https://example.org/v1", "model": "test"}}
        post = mock.Mock(return_value={"choices": [{"message": {"content": json.dumps(value)}}]})
        with mock.patch.dict("os.environ", {"LLM_API_KEY": "test"}):
            stats = relations.assess(papers, config, self.store, "test", post, json.loads)
            self.assertEqual(stats["review_requests"], 1)
            self.assertEqual(papers[0]["relation_decision"], "exclude")
            self.assertEqual(papers[1]["relation_decision"], "defer")
            stats = relations.assess(papers, config, self.store, "test", post, json.loads, allow_network=False)
            self.assertEqual(stats["review_cache_hits"], 1)
            self.assertEqual(post.call_count, 1)

    def test_translation_cache_cannot_override_admission(self):
        p = paper()
        config = {"enabled": True, "base_url": "https://example.org/v1", "model": "test"}
        value = {field: "safe text" for field in scout.ANALYSIS_STRING_FIELDS}
        value.update(tags=["RL", "methods"], main_conference_status="excluded", relation_decision="allow")
        self.store.save_translation(p["id"], scout.analysis_fingerprint(p, config), value)
        with mock.patch.dict("os.environ", {"LLM_API_KEY": "test"}), mock.patch.object(scout, "post_json") as post:
            scout.translate_papers([p], config, "test", store=self.store)
        post.assert_not_called()
        self.assertEqual(p["main_conference_status"], "verified")
        self.assertNotIn("relation_decision", p)

    def test_hard_gate_runs_before_metadata_and_semantic(self):
        p = acl()
        p["proceedings_id"] = "https://dblp.org/rec/conf/acl/2025-5"
        self.store.save_discovery_page("acl", 2025, [p], {})
        config = {"years": [2025], "explicit_interests": ["RL"], "semantic": {"enabled": True}, "relation_policy": {"enabled": True}}
        args = argparse.Namespace(config=Path(self.temp.name) / "config.json", output=Path(self.temp.name) / "report.md",
                                  labels=None, no_update_seen=True, discover_only=False, evaluate_only=False)
        venues = {"acl": {"type": "conference"}}
        run = self.store.start_run("test")
        with mock.patch.object(scout.discovery, "discover", return_value=({}, [])), mock.patch.object(scout.metadata, "enrich", return_value={}) as enrich, mock.patch.object(scout.semantic, "rank") as rank:
            scout.run_pipeline_body(args, config, "test", [p], [], self.store, run, Path(self.temp.name) / "seen.json", set(), ["acl"], venues)
        self.assertEqual(enrich.call_args.args[0], [])
        rank.assert_not_called()
        diagnostic = json.loads(args.output.with_suffix(".diagnostics.json").read_text(encoding="utf-8"))
        self.assertEqual(diagnostic["qualification"]["statuses"], {"excluded": 1})
        self.assertEqual(diagnostic["selected_ids"], [])

    def test_positive_pipeline_keeps_rl_and_blocks_pure_llm_posttraining(self):
        traditional = acl()
        posttraining = acl()
        posttraining.update(id="conf/acl/posttraining", title="Reinforcement learning for language models",
                            abstract=role_review()["llm_role"]["quote"])
        config = {"years": [2025], "explicit_interests": ["reinforcement learning"],
                  "topic_priority": {"primary_topics": ["reinforcement learning"]},
                  "relation_policy": {"enabled": True, "mode": "production"},
                  "llm_translation": {"enabled": False, "base_url": "https://example.org/v1", "model": "test"}}
        response = {"choices": [{"message": {"content": json.dumps(role_review())}}]}
        with mock.patch.dict("os.environ", {"LLM_API_KEY": "test"}):
            relations.assess([posttraining], config, self.store, "test", mock.Mock(return_value=response), json.loads)
        self.store.save_discovery_page("acl", 2025, [traditional, posttraining], {})
        args = argparse.Namespace(config=Path(self.temp.name) / "config.json", output=Path(self.temp.name) / "positive.md",
                                  labels=None, no_update_seen=True, discover_only=False, evaluate_only=True)
        run = self.store.start_run("test")
        with mock.patch.object(scout, "post_json") as post, mock.patch.object(scout, "send_email") as send:
            scout.run_pipeline_body(args, config, "test", [traditional], [], self.store, run, Path(self.temp.name) / "seen.json", set(), ["acl"], {"acl": {"type": "conference"}})
        post.assert_not_called()
        send.assert_not_called()
        diagnostic = json.loads(args.output.with_suffix(".diagnostics.json").read_text(encoding="utf-8"))
        self.assertEqual(diagnostic["selected_ids"], [traditional["id"]])
        denied = next(p for p in diagnostic["papers"] if p["id"] == posttraining["id"])
        self.assertEqual(denied["selection_reason"], "relation_exclude")
        self.assertEqual(self.store.delivered_ids(), set())

    def test_actions_config_exposes_independent_relation_mode(self):
        from scripts.build_actions_config import build
        from ccf_paper_scout.cli import load_config
        root = Path(self.temp.name)
        env = {"SMTP_SENDER": "sender@example.org", "SMTP_RECEIVER": "receiver@example.org",
               "LLM_BASE_URL": "https://example.org/v1", "LLM_MODEL": "test", "RELATION_MODE": "production"}
        with mock.patch.dict("os.environ", env, clear=True):
            build(Path("config.example.json"), root / "action.json", root, 10, False)
        config = load_config(root / "action.json")
        self.assertFalse(config["delivery"]["smtp"]["enabled"])
        self.assertEqual(config["relation_policy"]["mode"], "production")
        self.assertFalse(config["semantic"]["enabled"])

    def test_human_evaluation_does_not_invent_missing_labels(self):
        from scripts.evaluate_roles import evaluate
        diagnostic = {"papers": [{"id": "a", "main_conference_status": "verified", "relation_decision": "exclude",
                                   "relation_review": {"status": "classified", "labels": ["rl_for_llm"]}}],
                      "relation_policy_selected_ids": ["unknown"]}
        labels = [{"id": "a", "eligibility": "excluded", "wanted": True, "substantial_marl": True, "relations": ["llm_for_rl"]}]
        result = evaluate(diagnostic, labels)
        self.assertEqual(result["unlabeled_selected"], 1)
        self.assertIsNone(result["precision_labeled_selected"])
        self.assertEqual(result["eligibility_false_admissions"], 1)
        self.assertEqual(result["marl_false_exclusions"], 1)


if __name__ == "__main__":
    unittest.main()
