from __future__ import annotations

import datetime as dt
import re
import xml.etree.ElementTree as ET
import urllib.parse
import urllib.request
import time
from email.utils import parsedate_to_datetime

from ..identity import canonical_key
from ..models import Paper, SourceEvidence
from .base import SourceBatch

ATOM = "http://www.w3.org/2005/Atom"
ARXIV = "http://arxiv.org/schemas/atom"
NS = {"a": ATOM, "arxiv": ARXIV}


def base_arxiv_id(value: str) -> tuple[str, str]:
    value = value.strip().rsplit("/abs/", 1)[-1]
    match = re.match(r"^(.*?)(v\d+)?$", value, flags=re.I)
    assert match
    return match.group(1), match.group(2) or ""


def _text(entry: ET.Element, path: str) -> str:
    node = entry.find(path, NS)
    return " ".join((node.text or "").split()) if node is not None else ""


def parse_arxiv_atom(xml_text: str, *, reject_withdrawn: bool = True) -> list[Paper]:
    root = ET.fromstring(xml_text)
    papers: list[Paper] = []
    for entry in root.findall("a:entry", NS):
        raw_id = _text(entry, "a:id")
        arxiv_id, version = base_arxiv_id(raw_id)
        title = _text(entry, "a:title")
        abstract = _text(entry, "a:summary")
        if not arxiv_id or not title:
            continue
        withdrawn = bool(re.search(r"\b(withdrawn|retracted)\b", abstract, flags=re.I))
        if withdrawn and reject_withdrawn:
            continue
        doi = _text(entry, "arxiv:doi").lower()
        identifiers = {"arxiv": arxiv_id}
        if doi:
            identifiers["doi"] = doi
        categories = [node.attrib.get("term", "") for node in entry.findall("a:category", NS) if node.attrib.get("term")]
        primary = entry.find("arxiv:primary_category", NS)
        primary_category = primary.attrib.get("term", "") if primary is not None else (categories[0] if categories else "")
        authors = [_text(author, "a:name") for author in entry.findall("a:author", NS)]
        pdf = ""
        for link in entry.findall("a:link", NS):
            if link.attrib.get("title") == "pdf" or link.attrib.get("type") == "application/pdf":
                pdf = link.attrib.get("href", "")
                break
        status = "withdrawn" if withdrawn else "preprint"
        papers.append(Paper(
            canonical_id=canonical_key(identifiers), title=title, abstract=abstract, authors=authors,
            publication_status=status, publication_year=int(_text(entry, "a:published")[:4] or 0) or None,
            identifiers=identifiers, sources=[SourceEvidence("arxiv", arxiv_id, "author-preprint", raw_id)],
            categories=categories, primary_category=primary_category, source_version=version,
            updated_at=_text(entry, "a:updated"), published_at=_text(entry, "a:published"),
            url=f"https://arxiv.org/abs/{arxiv_id}", pdf_url=pdf,
            channel="preprint",
        ))
    return papers


class ArxivSource:
    source_id = "arxiv"

    def discover(self, request: dict, cursor: str | None = None) -> SourceBatch:
        categories = [str(value) for value in request.get("categories", [])]
        topics = [str(value) for value in request.get("topics", [])]
        category_query = " OR ".join(f"cat:{category}" for category in categories)
        topic_query = " OR ".join(f'all:"{topic}"' for topic in topics)
        parts = [f"({category_query})" if category_query else "", f"({topic_query})" if topic_query else ""]
        search_query = " AND ".join(part for part in parts if part) or "all:*"
        start = int(cursor or 0)
        page_size = max(1, min(100, int(request.get("page_size", 50))))
        params = urllib.parse.urlencode({
            "search_query": search_query,
            "start": start,
            "max_results": page_size,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        })
        req = urllib.request.Request(
            "https://export.arxiv.org/api/query?" + params,
            headers={"User-Agent": str(request.get("user_agent", "ccf-paper-scout/0.3"))},
        )
        attempts = int(request.get("max_attempts", 3))
        delay = float(request.get("request_delay_seconds", 3.0))
        for attempt in range(attempts):
            if cursor is not None and delay > 0:
                time.sleep(delay)
            try:
                with urllib.request.urlopen(req, timeout=int(request.get("timeout_seconds", 60))) as response:
                    text = response.read().decode("utf-8")
                break
            except urllib.error.HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt + 1 >= attempts:
                    raise
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                wait = delay * (2 ** attempt)
                if retry_after:
                    try:
                        wait = float(retry_after)
                    except ValueError:
                        retry_at = parsedate_to_datetime(retry_after)
                        if retry_at.tzinfo is None:
                            retry_at = retry_at.replace(tzinfo=dt.timezone.utc)
                        wait = max(0.0, (retry_at - dt.datetime.now(dt.timezone.utc)).total_seconds())
                time.sleep(wait)
            except (OSError, TimeoutError):
                if attempt + 1 >= attempts:
                    raise
                time.sleep(delay * (2 ** attempt))
        root = ET.fromstring(text)
        raw_count = len(root.findall("a:entry", NS))
        records = parse_arxiv_atom(text, reject_withdrawn=bool(request.get("reject_withdrawn", True)))
        max_age_days = int(request.get("max_age_days", 0))
        if max_age_days > 0:
            cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=max_age_days)
            filtered: list[Paper] = []
            for paper in records:
                try:
                    submitted = dt.datetime.fromisoformat(paper.published_at.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if submitted >= cutoff:
                    filtered.append(paper)
            records = filtered
        next_cursor = str(start + raw_count) if raw_count == page_size else None
        return SourceBatch(records=records, next_cursor=next_cursor, raw_count=raw_count, exhausted=next_cursor is None)
