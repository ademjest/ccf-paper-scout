"""Evaluate independently human-labeled role/eligibility examples; never approve calibration."""
from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path

from ccf_paper_scout.relations import PROTOTYPES


def evaluate(diagnostic: dict, labels: list[dict]) -> dict:
    papers = {p["id"]: p for p in [*diagnostic.get("papers", []), *diagnostic.get("qualification_deferred", [])]}
    selected = set(diagnostic.get("relation_policy_selected_ids", []))
    result = {"labeled": len(labels), "found": 0, "selected_labeled": 0, "selected_unwanted": 0,
              "eligibility_false_admissions": 0, "eligibility_unresolved": 0,
              "marl_false_exclusions": 0, "marl_deferred": 0, "relation_unresolved": 0}
    confusion = collections.Counter()
    seen = set()
    for label in labels:
        if (not isinstance(label, dict) or not isinstance(label.get("id"), str) or label["id"] in seen or
                label.get("eligibility") not in ("verified", "excluded", "unresolved") or
                type(label.get("wanted")) is not bool or type(label.get("substantial_marl")) is not bool or
                not isinstance(label.get("relations"), list) or any(value not in PROTOTYPES for value in label["relations"])):
            raise ValueError("invalid/duplicate human label; see docs/main-conference-and-relations-implementation.md")
        seen.add(label["id"])
        paper = papers.get(label["id"])
        if not paper:
            continue
        result["found"] += 1
        result["selected_labeled"] += label["id"] in selected
        result["selected_unwanted"] += label["id"] in selected and not label["wanted"]
        result["eligibility_false_admissions"] += paper.get("main_conference_status") == "verified" and label["eligibility"] == "excluded"
        result["eligibility_unresolved"] += paper.get("main_conference_status") == "unresolved"
        review = paper.get("relation_review") or {}
        result["relation_unresolved"] += review.get("status") == "uncertain"
        result["marl_false_exclusions"] += label["substantial_marl"] and paper.get("relation_decision") == "exclude"
        result["marl_deferred"] += label["substantial_marl"] and paper.get("relation_decision") == "defer"
        actual = "+".join(sorted(label["relations"])) or "none"
        predicted = "+".join(sorted(review.get("labels", []))) or review.get("status", "not_assessed")
        confusion[actual + " -> " + predicted] += 1
    result["unmatched_labels"] = len(labels) - result["found"]
    result["unlabeled_selected"] = len(selected - seen)
    result["precision_labeled_selected"] = (1 - result["selected_unwanted"] / result["selected_labeled"]) if result["selected_labeled"] else None
    result["relation_confusion"] = dict(confusion)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--diagnostics", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(evaluate(json.loads(args.diagnostics.read_text(encoding="utf-8")),
                              json.loads(args.labels.read_text(encoding="utf-8"))), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
