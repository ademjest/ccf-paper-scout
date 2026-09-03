from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

from ..identity import canonical_key
from ..models import Paper, SourceEvidence
from .base import SourceBatch


def parse_ieee_payload(payload: dict) -> list[Paper]:
    papers: list[Paper] = []
    for article in payload.get("articles", []) or []:
        record_id = str(article.get("article_number", "")).strip()
        title = " ".join(str(article.get("title", "")).split())
        if not record_id or not title:
            continue
        doi = str(article.get("doi", "")).strip().lower()
        identifiers = {"ieee": record_id}
        if doi:
            identifiers["doi"] = doi
        authors_payload = article.get("authors", {}).get("authors", []) if isinstance(article.get("authors"), dict) else []
        authors = [str(author.get("full_name", "")).strip() for author in authors_payload if isinstance(author, dict) and author.get("full_name")]
        status = "online_first" if str(article.get("content_type", "")).lower() == "early access articles" else "published"
        papers.append(Paper(
            canonical_id=canonical_key(identifiers), title=title,
            abstract=" ".join(str(article.get("abstract", "")).split()), authors=authors,
            publication_status=status,
            publication_year=int(article["publication_year"]) if str(article.get("publication_year", "")).isdigit() else None,
            venue_name=str(article.get("publication_title", "")).strip(), publisher="IEEE",
            identifiers=identifiers,
            sources=[SourceEvidence("ieee_xplore", record_id, "publisher-record", str(article.get("html_url", "")))],
            url=str(article.get("html_url", "")), channel="formal",
        ))
    return papers


class IeeeXploreSource:
    source_id = "ieee_xplore"

    def discover(self, request: dict, cursor: str | None = None) -> SourceBatch:
        env_name = str(request.get("api_key_env", "IEEE_XPLORE_API_KEY"))
        key = os.environ.get(env_name, "")
        if not key:
            raise RuntimeError("IEEE_XPLORE_API_KEY is required")
        topics = [str(topic) for topic in request.get("topics", [])]
        query = " OR ".join(topics)
        start = max(1, int(cursor or 1))
        page_size = max(1, min(200, int(request.get("page_size", 50))))
        params = urllib.parse.urlencode({
            "apikey": key, "format": "json", "max_records": page_size,
            "start_record": start, "sort_order": "desc", "sort_field": "publication_year",
            "querytext": query,
        })
        req = urllib.request.Request("https://ieeexploreapi.ieee.org/api/v1/search/articles?" + params)
        with urllib.request.urlopen(req, timeout=int(request.get("timeout_seconds", 60))) as response:
            payload = json.load(response)
        records = parse_ieee_payload(payload)
        raw_count = len(payload.get("articles", []) or [])
        total = int(payload.get("total_records", raw_count) or raw_count)
        next_cursor = str(start + raw_count) if raw_count and start + raw_count <= total else None
        return SourceBatch(records=records, next_cursor=next_cursor, raw_count=raw_count, exhausted=next_cursor is None)
