"""Stable aliases and conservative matching; never replace a historical delivery ID."""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit


def doi(value: str) -> str:
    value = re.sub(r'^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)', '', str(value or '').strip(), flags=re.I)
    return value.lower() if re.fullmatch(r'10\.\d{4,9}/\S+', value, re.I) else ''


def title(value: str) -> str:
    return ' '.join(re.findall(r'\w+', unicodedata.normalize('NFKC', value or '').casefold()))


def url(value: str) -> str:
    parsed = urlsplit(str(value or ''))
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        return ''
    return urlunsplit(('https', parsed.netloc.lower(), parsed.path.rstrip('/'), parsed.query, ''))


def aliases(paper: dict) -> set[tuple[str, str]]:
    result = set()
    identifier = paper.get('id', '')
    if identifier.startswith(('conf/', 'journals/')):
        result.add(('dblp', identifier))
    if paper.get('source') and paper.get('source_record_id'):
        result.add((paper['source'], str(paper['source_record_id'])))
    value = doi(paper.get('doi')) or doi(paper.get('ee'))
    if value:
        result.add(('doi', value))
    for value in (paper.get('url'), paper.get('ee')):
        value = url(value)
        if value:
            result.add(('url', value))
            match = re.search(r'arxiv\.org/(?:abs|pdf)/(.+?)(?:\.pdf)?$', value)
            if match:
                result.add(('arxiv', re.sub(r'v\d+$', '', match[1])))
            match = re.search(r'dblp\.org/rec/((?:conf|journals)/[^?#]+)', value)
            if match:
                result.add(('dblp', match[1]))
    for namespace, value in paper.get('external_ids', {}).items():
        if value:
            normalized = doi(value) if namespace == 'doi' else re.sub(r'v\d+$', '', value) if namespace == 'arxiv' else value
            if normalized:
                result.add((namespace, normalized))
    return result
