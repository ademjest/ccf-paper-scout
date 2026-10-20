"""Evidence-based proceedings admission. Unknown volumes and tracks fail closed."""
from __future__ import annotations

import collections
import datetime as dt
import json
import re
from importlib.resources import files
from typing import Any, Callable

FIELDS = ("proceedings_id", "track", "main_conference_status", "verification_source", "exclusion_reason", "qualification_version")


def registry() -> dict[str, Any]:
    return json.loads(files("ccf_paper_scout").joinpath("data/main_proceedings.json").read_text(encoding="utf-8"))


def qualify(paper: dict[str, Any], catalog: dict[str, Any]) -> str:
    paper.update(track="unknown", main_conference_status="unresolved", verification_source="", exclusion_reason="unknown_proceedings", qualification_version=catalog["version"])
    identifier = paper.get("proceedings_id", "")
    entry = catalog["volumes"].get(identifier)
    if not entry or entry["year"] != paper.get("year") or not paper["id"].startswith("conf/" + entry["venue"] + "/"):
        return "unresolved"
    paper["verification_source"] = entry["source"]
    paper["track"] = entry["track"]
    status = entry["status"]
    # Mixed volumes require an individually audited track membership, not a guessed title/DOI.
    if status == "mixed":
        membership = catalog.get("papers", {}).get(paper["id"], {})
        if entry.get("main_document_pattern") and re.fullmatch(entry["main_document_pattern"], str(paper.get("ee", ""))):
            membership = {"proceedings_id": identifier, "status": "verified", "track": "main_research", "source": entry["source"]}
        if entry["venue"] == "aaai":
            # Official volume/issue mapping, NOT the article-number suffix or DOI year.
            issue = re.search(r"(?:^|/)10\.1609/(aaai\.v\d+i\d+)\.\d+$", str(paper.get("doi", "")).lower())
            if issue:
                membership = catalog.get("publisher_issues", {}).get(issue[1], membership)
        status = membership.get("status", "unresolved") if membership.get("proceedings_id") == identifier and membership.get("source") else "unresolved"
        if status in ("verified", "excluded"):
            paper["track"] = membership["track"]
            paper["verification_source"] = membership["source"]
    paper["main_conference_status"] = status
    paper["exclusion_reason"] = "" if status == "verified" else "excluded_track" if status == "excluded" else "track_not_verified"
    # A verified volume cannot make explicitly non-research content eligible.
    if status == "verified" and (re.match(r"^(preface|editorial|erratum)\b", paper.get("title", ""), re.I) or
                                 re.search(r"\b(?:this|the present) (?:paper|work|article) (?:is|presents|provides) (?:an? )?tutorial\b|\b(?:in this|this|our) tutorial\b", paper.get("abstract", ""), re.I)):
        paper.update(main_conference_status="excluded", exclusion_reason="non_research_content")
    return paper["main_conference_status"]


def backfill(papers: list[dict[str, Any]], store: Any, settings: dict[str, Any], lookup: Callable) -> dict[str, int]:
    """Bounded migration of old candidates without proceedings metadata; no cursor reset."""
    boundary = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)).isoformat()
    due = [p for p in papers if not p.get("proceedings_id") and p.get("proceedings_checked_at", "") < boundary]
    due.sort(key=lambda paper: (paper.get("proceedings_checked_at", ""), paper["id"]))
    maximum = max(0, int(settings.get("max_backfill_papers_per_run", 1000)))
    checked = failures = 0
    for offset in range(0, min(len(due), maximum), 100):
        batch = due[offset:min(offset + 100, maximum)]
        try:
            values = lookup([p["id"] for p in batch])
        except RuntimeError:
            failures += 1
            break
        for paper in batch:
            paper.update(values.get(paper["id"], {}))
            paper["proceedings_checked_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        store.update_candidates(batch)
        checked += len(batch)
    return {"checked": checked, "remaining": len(due) - checked, "failed_batches": failures}


def gate(papers: list[dict[str, Any]], store: Any, catalog: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], dict[str, int]]:
    catalog = catalog or registry()
    counts: collections.Counter = collections.Counter()
    for paper in papers:
        counts[qualify(paper, catalog)] += 1
        if paper["main_conference_status"] != "verified":
            paper["selection_reason"] = "main_conference_" + paper["exclusion_reason"]
    store.update_candidates(papers)
    return [p for p in papers if p["main_conference_status"] == "verified"], dict(counts)
