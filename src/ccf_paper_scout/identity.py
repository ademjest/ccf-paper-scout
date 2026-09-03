from __future__ import annotations

import re
from collections.abc import Iterable

from .models import Paper


def _base_arxiv(value: str) -> str:
    return re.sub(r"v\d+$", "", value.strip(), flags=re.I).lower()


def canonical_key(identifiers: dict[str, str]) -> str:
    doi = str(identifiers.get("doi", "")).strip().lower()
    if doi:
        return "doi:" + doi
    arxiv = str(identifiers.get("arxiv", "")).strip()
    if arxiv:
        return "arxiv:" + _base_arxiv(arxiv)
    for scheme in ("ieee", "dblp", "openalex"):
        value = str(identifiers.get(scheme, "")).strip().lower()
        if value:
            return f"{scheme}:{value}"
    return ""


def merge_papers(papers: Iterable[Paper]) -> list[Paper]:
    merged: dict[str, Paper] = {}
    status_priority = {"unknown": 0, "preprint": 1, "accepted": 2, "online_first": 3, "published": 4}
    for paper in papers:
        key = canonical_key(paper.identifiers) or paper.canonical_id
        current = merged.get(key)
        if current is None:
            paper.canonical_id = key
            merged[key] = paper
            continue
        current.identifiers.update(paper.identifiers)
        known_sources = {(source.source, source.source_record_id) for source in current.sources}
        current.sources.extend(source for source in paper.sources if (source.source, source.source_record_id) not in known_sources)
        if not current.abstract and paper.abstract:
            current.abstract = paper.abstract
        if status_priority.get(paper.publication_status, 0) > status_priority.get(current.publication_status, 0):
            current.publication_status = paper.publication_status
            if paper.title:
                current.title = paper.title
            current.venue_id = paper.venue_id or current.venue_id
            current.venue_name = paper.venue_name or current.venue_name
            current.publisher = paper.publisher or current.publisher
    return list(merged.values())
