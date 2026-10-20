"""Bounded, cached abstract enrichment for the persistent candidate pool."""
from __future__ import annotations

import os
import urllib.error
import urllib.parse
from typing import Any, Callable


def enrich(papers: list[dict[str, Any]], store: Any, config: dict[str, Any], user_agent: str,
           request_json: Callable, normalize_doi: Callable, decode_abstract: Callable) -> dict[str, int]:
    budget = max(0, int(config.get("max_requests_per_run", 200)))
    fraction = float(config.get("exploration_fraction", 0.2))
    if not 0 <= fraction <= 1:
        raise ValueError("metadata.exploration_fraction must be between 0 and 1")
    due = [paper for paper in papers if not paper.get("abstract") and store.metadata_due(paper["id"])]
    # Rotate a fraction through the least recently discovered candidates, independent of title score.
    exploration_count = int(budget * fraction)
    exploration = sorted(due, key=lambda p: (p.get("first_seen_at", ""), p["id"]))[:exploration_count]
    explored = {p["id"] for p in exploration}
    ordered = exploration + [paper for paper in due if paper["id"] not in explored]
    stats = {"requests": 0, "enriched": 0, "missing": 0, "failed": 0}
    for paper in ordered:
        if stats["requests"] >= budget:
            break
        doi = normalize_doi(paper.get("doi", "")) or normalize_doi(paper.get("ee", ""))
        if not doi:
            store.save_metadata(paper, "no_doi")
            continue
        stats["requests"] += 1
        throttled = False
        url = "https://api.openalex.org/works/" + urllib.parse.quote("https://doi.org/" + doi, safe="")
        api_key = os.environ.get(str(config.get("api_key_env", "OPENALEX_API_KEY")), "")
        if api_key:
            url += "?" + urllib.parse.urlencode({"api_key": api_key})
        try:
            payload = request_json(url, user_agent)
            abstract = decode_abstract(payload.get("abstract_inverted_index"))
            paper["doi"] = doi
            paper["abstract"] = abstract
            paper["openalex_id"] = payload.get("id", "")
            if abstract:
                paper["abstract_source"] = "OpenAlex"
                stats["enriched"] += 1
                status = "success"
            else:
                stats["missing"] += 1
                status = "missing"
        except (RuntimeError, urllib.error.URLError, TimeoutError, ValueError, KeyError, TypeError) as exc:
            throttled = getattr(exc, "code", None) == 429
            status = "missing" if getattr(exc, "code", None) in (400, 404) else "failed"
            stats["missing" if status == "missing" else "failed"] += 1
        store.save_metadata(paper, status)
        if throttled:
            break
    return stats
