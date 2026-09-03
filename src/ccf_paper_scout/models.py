from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SourceEvidence:
    source: str
    source_record_id: str
    evidence_type: str
    url: str
    fetched_at: str = ""


@dataclass
class Paper:
    canonical_id: str
    title: str
    abstract: str = ""
    authors: list[str] = field(default_factory=list)
    publication_status: str = "unknown"
    publication_year: int | None = None
    venue_id: str | None = None
    venue_name: str = ""
    publisher: str = ""
    identifiers: dict[str, str] = field(default_factory=dict)
    sources: list[SourceEvidence] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    primary_category: str = ""
    source_version: str = ""
    first_seen_at: str = ""
    updated_at: str = ""
    published_at: str = ""
    url: str = ""
    pdf_url: str = ""
    score: float = 0.0
    channel: str = "other"
