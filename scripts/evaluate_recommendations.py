"""Evaluate private diagnostics and propose, but never auto-approve, semantic thresholds."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from ccf_paper_scout.selection import metrics


def fit_threshold(rows: list[dict], labels: dict[str, bool], target: float) -> float:
    labeled = [row for row in rows if row["id"] in labels and isinstance(row.get("semantic_score"), (int, float))]
    if sum(labels[row["id"]] for row in labeled) < 3 or sum(not labels[row["id"]] for row in labeled) < 3:
        raise ValueError("each text modality needs at least 3 positive and 3 negative calibration examples")
    choices = []
    for threshold in sorted({row["semantic_score"] for row in labeled}):
        selected = [row for row in labeled if row["semantic_score"] >= threshold]
        result = metrics(selected, labels)
        if result["precision"] is not None and result["precision"] >= target:
            choices.append((sum(labels[row["id"]] for row in selected), -threshold, threshold))
    if not choices:
        raise ValueError("no threshold meets target precision; improve the profile or training examples")
    return max(choices)[2]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--diagnostics", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True, help="private calibration labels (paper ID -> bool)")
    parser.add_argument("--test-labels", type=Path, help="disjoint held-out labels")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--calibrate", action="store_true")
    parser.add_argument("--target-precision", type=float, default=0.8)
    args = parser.parse_args()
    diagnostics = json.loads(args.diagnostics.read_text(encoding="utf-8"))
    labels = json.loads(args.labels.read_text(encoding="utf-8"))
    if not 0 < args.target_precision <= 1:
        parser.error("target precision must be between 0 and 1")
    # Also validates bool labels before fitting.
    result = {"current_selection": metrics([{"id": key} for key in diagnostics["selected_ids"]], labels)}
    if args.calibrate:
        stats = diagnostics["semantic"]
        if not stats:
            parser.error("run evaluate with semantic enabled first")
        if not args.test_labels:
            parser.error("calibration proposal requires independent --test-labels")
        heldout = json.loads(args.test_labels.read_text(encoding="utf-8"))
        if labels.keys() & heldout.keys():
            parser.error("calibration and held-out paper IDs must be disjoint")
        rows = diagnostics["papers"]
        thresholds = {}
        for name, has_abstract in (("abstract", True), ("title_only", False)):
            group = [row for row in rows if row.get("has_abstract", False) == has_abstract]
            thresholds[name] = fit_threshold(group, labels, args.target_precision)
        selected = [row for row in rows if isinstance(row.get("semantic_score"), (int, float)) and
                    row["semantic_score"] >= thresholds["abstract" if row.get("has_abstract") else "title_only"]]
        result = {"approved": False, "model_signature": stats["model_signature"], "profile_hash": stats["profile_hash"],
                  "thresholds": thresholds, "target_precision": args.target_precision,
                  "calibration_metrics": metrics([row for row in selected if row["id"] in labels], labels),
                  "heldout_metrics": metrics([row for row in selected if row["id"] in heldout], heldout)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Private evaluation written; a calibration proposal requires human review before approved=true.")


if __name__ == "__main__":
    main()
