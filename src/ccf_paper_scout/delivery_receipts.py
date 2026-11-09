"""Durable SMTP acceptance evidence, independent of the candidate database."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import re


FILENAME = 'delivery-receipts.jsonl'


def atomic_json(path: Path, value) -> None:
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def validate(value: dict) -> dict:
    if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1:
        raise ValueError('unsupported delivery receipt')
    for key in ('run_id', 'accepted_at', 'report_sha256', 'evidence'):
        if not isinstance(value.get(key), str) or not value[key]:
            raise ValueError('missing delivery receipt field: ' + key)
    if not re.fullmatch(r'[a-f0-9]{64}', value['report_sha256']):
        raise ValueError('invalid receipt report hash')
    if dt.datetime.fromisoformat(value['accepted_at']).tzinfo is None:
        raise ValueError('receipt acceptance time must include timezone')
    if value['evidence'] not in ('smtp_accepted', 'recovered_from_actions_log'):
        raise ValueError('unknown receipt evidence')
    if value['evidence'] == 'recovered_from_actions_log' and not value.get('actions_run_id'):
        raise ValueError('recovered receipt requires Actions run evidence')
    ids = value.get('selected_paper_ids')
    if not isinstance(ids, list) or not ids or any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
        raise ValueError('invalid receipt paper IDs')
    aliases = value.get('paper_aliases', {})
    if not isinstance(aliases, dict) or any(i not in ids for i in aliases):
        raise ValueError('invalid receipt alias IDs')
    for pairs in aliases.values():
        if not isinstance(pairs, list) or any(not isinstance(pair, list) or len(pair) != 2 or
                                            any(not isinstance(v, str) or not v for v in pair) for pair in pairs):
            raise ValueError('invalid receipt aliases')
    return value


def load(root: Path) -> dict[str, dict]:
    path = root / FILENAME
    result = {}
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            value = validate(json.loads(line))
            previous = result.get(value['run_id'])
            if previous is not None and previous != value:
                raise ValueError('conflicting delivery receipts')
            result[value['run_id']] = value
    return result


def record(root: Path, value: dict) -> None:
    value = validate(value)
    records = load(root)
    previous = records.get(value['run_id'])
    if previous is not None:
        if previous != value:
            raise ValueError('conflicting delivery receipt')
        return
    records[value['run_id']] = value
    path = root / FILENAME
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(''.join(json.dumps(r, ensure_ascii=False, sort_keys=True) + '\n' for r in records.values()), encoding='utf-8')
    temporary.replace(path)


def from_marker(marker: dict) -> dict:
    if marker.get('phase') != 'smtp_accepted':
        raise ValueError('pending marker is not SMTP acceptance evidence')
    return validate(dict(version=1, run_id=marker['run_id'], accepted_at=marker['updated_at'],
                         actions_run_id=marker.get('actions_run_id', ''), evidence='smtp_accepted',
                         report_sha256=marker['report_sha256'], selected_paper_ids=marker['selected_paper_ids'],
                         paper_aliases=marker.get('paper_aliases', {})))


def reconcile(root: Path) -> dict[str, dict]:
    """Recover small records first; callers project them into SQLite separately."""
    marker_path = root / '.delivery-pending'
    marker = json.loads(marker_path.read_text(encoding='utf-8')) if marker_path.exists() else None
    if marker and marker.get('phase') == 'smtp_accepted':
        records = load(root)
        if marker['run_id'] not in records:
            record(root, from_marker(marker))
    records = load(root)
    matched = records.get(marker.get('run_id')) if marker else None
    if matched and (matched['selected_paper_ids'] != marker.get('selected_paper_ids') or
                    matched['report_sha256'] != marker.get('report_sha256')):
        raise ValueError('receipt does not match pending transaction')
    if records:
        seen_path = root / 'seen.json'
        seen = json.loads(seen_path.read_text(encoding='utf-8')) if seen_path.exists() else {'ids': []}
        ids = set(seen['ids'])
        for value in records.values():
            ids.update(value['selected_paper_ids'])
        seen['ids'] = sorted(ids)
        atomic_json(seen_path, seen)
    if matched:
        marker_path.unlink()
    return records
