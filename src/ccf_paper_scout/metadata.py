"""Bounded, cached abstract enrichment for the persistent candidate pool."""
from __future__ import annotations

import os
import time
import urllib.parse
from typing import Any, Callable

from . import official_abstracts

SOURCE_VERSION = "official-fallback-v1"


def enrich(papers: list[dict[str, Any]], store: Any, config: dict[str, Any], user_agent: str,
           request_json: Callable, normalize_doi: Callable, decode_abstract: Callable,
           *, fetch_text: Callable | None = None) -> dict[str, int]:
    budget = max(0, int(config.get("max_requests_per_run", 200)))
    fraction = float(config.get("exploration_fraction", 0.2))
    if not 0 <= fraction <= 1:
        raise ValueError("metadata.exploration_fraction must be between 0 and 1")
    fetch_text = fetch_text or official_abstracts.fetch
    invalid = [p for p in papers if official_abstracts.is_bibliographic_placeholder(p.get("abstract", ""))]
    for p in invalid:
        p["abstract"] = ""
        for key in ("abstract_hash", "abstract_source", "abstract_source_url"):
            p.pop(key, None)
        p["metadata_invalidated"] = "bibliographic_placeholder"
    if invalid:
        store.update_candidates(invalid)
    due = [p for p in papers if not p.get("abstract") and
           (store.metadata_due(p["id"]) or p.get("metadata_source_version") != SOURCE_VERSION)]
    # Rotate a fraction through the least recently discovered candidates, independent of title score.
    exploration_count = int(budget * fraction)
    exploration = sorted(due, key=lambda p: (p.get("first_seen_at", ""), p["id"]))[:exploration_count]
    explored = {p["id"] for p in exploration}
    ordered = exploration + [paper for paper in due if paper["id"] not in explored]
    stats = {"requests": 0, "enriched": 0, "missing": 0, "failed": 0, "invalidated": len(invalid)}
    blocked, last_request = set(), {}
    for paper in ordered:
        if stats["requests"] >= budget:
            break
        doi = normalize_doi(paper.get("doi", "")) or normalize_doi(paper.get("ee", ""))
        official = official_abstracts.official_url(paper) if config.get("official_sources", True) else None
        routes = [("official", official)] if official else []
        if doi:
            routes.append(("OpenAlex", "https://api.openalex.org/works/" + urllib.parse.quote("https://doi.org/" + doi, safe="")))
        if not routes:
            paper["metadata_source_version"] = SOURCE_VERSION
            store.save_metadata(paper, "no_doi")
            continue
        attempts, status = [], "missing"
        for source, source_url in routes:
            host = urllib.parse.urlsplit(source_url).hostname
            if host in blocked or stats["requests"] >= budget:
                continue
            time.sleep(max(0, 1 - (time.monotonic() - last_request.get(host, 0))))
            stats["requests"] += 1
            attempt = {"source": source, "url": source_url}
            attempts.append(attempt)
            try:
                if source == "official":
                    abstract = official_abstracts.parse_abstract(fetch_text(source_url, user_agent), paper)
                else:
                    request_url = source_url
                    api_key = os.environ.get(str(config.get("api_key_env", "OPENALEX_API_KEY")), "")
                    if api_key:
                        request_url += "?" + urllib.parse.urlencode({"api_key": api_key})
                    payload = request_json(request_url, user_agent)
                    if normalize_doi(payload.get("doi", "")) != doi:
                        raise ValueError("openalex_doi_mismatch")
                    abstract = decode_abstract(payload.get("abstract_inverted_index"))
                    if official_abstracts.is_bibliographic_placeholder(abstract):
                        raise ValueError("bibliographic_placeholder")
                    paper.update(doi=doi, openalex_id=payload.get("id", ""))
                status = attempt["status"] = "success" if abstract else "missing"
                if abstract:
                    paper.update(abstract=abstract, abstract_source=source, abstract_source_url=source_url)
                    break
            except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
                code = getattr(exc, "code", None)
                status = "missing" if code in (400, 404) else "failed"
                # Exceptions can contain credential-bearing URLs; persist only type/status.
                attempt.update(status=status, error_type=type(exc).__name__, http_status=code)
                if code == 429:
                    blocked.add(host)
            finally:
                last_request[host] = time.monotonic()
        if attempts:
            paper.update(metadata_source_version=SOURCE_VERSION, metadata_attempts=attempts)
            stats["enriched" if status == "success" else status] += 1
            store.save_metadata(paper, status)
    return stats
