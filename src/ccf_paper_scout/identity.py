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


def _identity_aliases(paper: Paper) -> set[str]:
    aliases = {paper.canonical_id.lower()} if paper.canonical_id else set()
    for scheme, raw in paper.identifiers.items():
        value = str(raw).strip().lower()
        if not value:
            continue
        if scheme == "arxiv":
            value = _base_arxiv(value)
        aliases.add(f"{scheme}:{value}")
    return aliases


def _combine(current: Paper, paper: Paper) -> None:
    status_priority = {"unknown": 0, "preprint": 1, "accepted": 2, "online_first": 3, "published": 4}
    current.identifiers.update(paper.identifiers)
    known_sources = {(source.source, source.source_record_id) for source in current.sources}
    current.sources.extend(source for source in paper.sources if (source.source, source.source_record_id) not in known_sources)
    if not current.abstract and paper.abstract:
        current.abstract = paper.abstract
    if not current.authors and paper.authors:
        current.authors = paper.authors
    if status_priority.get(paper.publication_status, 0) > status_priority.get(current.publication_status, 0):
        current.publication_status = paper.publication_status
        if paper.title:
            current.title = paper.title
        current.publication_year = paper.publication_year or current.publication_year
        current.venue_id = paper.venue_id or current.venue_id
        current.venue_name = paper.venue_name or current.venue_name
        current.publisher = paper.publisher or current.publisher
        current.url = paper.url or current.url
        current.channel = paper.channel or current.channel


def merge_papers(papers: Iterable[Paper]) -> list[Paper]:
    merged: list[Paper] = []
    alias_to_paper: dict[str, Paper] = {}
    for paper in papers:
        aliases = _identity_aliases(paper)
        matches: list[Paper] = []
        for alias in aliases:
            match = alias_to_paper.get(alias)
            if match is not None and all(match is not existing for existing in matches):
                matches.append(match)
        if not matches:
            paper.canonical_id = canonical_key(paper.identifiers) or paper.canonical_id
            merged.append(paper)
            current = paper
        else:
            current = matches[0]
            _combine(current, paper)
            for duplicate in matches[1:]:
                _combine(current, duplicate)
                if duplicate in merged:
                    merged.remove(duplicate)
            current.canonical_id = canonical_key(current.identifiers) or current.canonical_id
        current_aliases = _identity_aliases(current) | aliases
        for duplicate in matches[1:]:
            current_aliases |= _identity_aliases(duplicate)
        for alias in current_aliases:
            alias_to_paper[alias] = current
    return merged
