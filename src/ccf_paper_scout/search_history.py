"""Durable search snapshots and query-specific judgments, separate from delivery."""
from __future__ import annotations

import hashlib
import json
import unicodedata

from state_store import now

TERMINAL = ('complete', 'cancelled', 'failed')


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


def query_key(query):
    return digest(' '.join(unicodedata.normalize('NFKC', query).casefold().split()))


def start(store, identifier, request):
    key = query_key(request['query'])
    split = request.get('split', 'dev')
    if split not in ('dev', 'test'):
        raise ValueError('split must be dev or test')
    with store.connection:
        store.connection.execute('BEGIN IMMEDIATE')
        row = store.connection.execute('SELECT split FROM search_sessions WHERE query_key=? LIMIT 1', (key,)).fetchone()
        if row and row[0] != split:
            raise ValueError('This query already belongs to ' + row[0] + '; keep its evaluation split')
        payload = dict(id=identifier, query=request['query'], request=request, query_key=key,
                       split=split, state='starting', results=[], discovery={})
        store.connection.execute('INSERT INTO search_sessions VALUES (?,?,?,?,?,?)',
            (identifier, key, split, now(), 'starting', json.dumps(payload, ensure_ascii=False)))


def save(store, result):
    # Store only retrieval context, never arbitrary application config or credentials.
    payload = dict(result)
    payload['results'] = [{k: v for k, v in p.items() if k not in ('feedback', 'judgments')} for p in result['results']]
    if result['state'] in TERMINAL:
        payload['snapshot_hash'] = digest({'request': payload['request'], 'results': payload['results']})
    with store.connection:
        cursor = store.connection.execute('UPDATE search_sessions SET state=?,payload_json=? WHERE search_id=? AND state NOT IN (?,?,?)',
            (result['state'], json.dumps(payload, ensure_ascii=False), result['id'], *TERMINAL))
        if cursor.rowcount != 1:
            raise ValueError('search missing or already finalized')


def get(store, identifier):
    row = store.connection.execute('SELECT payload_json,started_at FROM search_sessions WHERE search_id=?', (identifier,)).fetchone()
    if not row:
        return None
    result = json.loads(row[0])
    result['started_at'] = row[1]
    judgments = {}
    for paper_id, origin, payload in store.connection.execute(
            'SELECT paper_id,origin,payload_json FROM query_feedback WHERE search_id=?', (identifier,)):
        judgments.setdefault(paper_id, {})[origin] = json.loads(payload)
    bookmarks = {r[0]: json.loads(r[1]) for r in store.connection.execute('SELECT paper_id,payload_json FROM paper_feedback')}
    for paper in result['results']:
        paper['judgments'] = judgments.get(paper['id'], {})
        paper['feedback'] = {k: v for k, v in bookmarks.get(paper['id'], {}).items() if k in ('read', 'saved')}
    return result


def recent(store, limit=50):
    rows = store.connection.execute('SELECT search_id,started_at,state,payload_json FROM search_sessions ORDER BY started_at DESC LIMIT ?', (limit,))
    return [dict(id=i, started_at=t, state=s, query=(p := json.loads(raw))['query'],
                 split=p['split'], count=len(p['results'])) for i, t, s, raw in rows]


def label(store, identifier, paper_id, grade, *, origin='human', reason='', actor='user', snapshot_hash=None):
    if origin not in ('human', 'ai') or (grade is not None and (type(grade) is not int or grade not in (0, 1, 2))):
        raise ValueError('grade must be 0, 1, 2 or null')
    if not isinstance(reason, str) or len(reason) > 2000 or not isinstance(actor, str) or not 1 <= len(actor) <= 200:
        raise ValueError('invalid annotation provenance')
    result = get(store, identifier)
    if not result or result['state'] not in TERMINAL or paper_id not in {p['id'] for p in result['results']}:
        raise ValueError('label a paper from a completed search snapshot')
    if snapshot_hash is not None and snapshot_hash != result['snapshot_hash']:
        raise ValueError('snapshot changed')
    if origin == 'ai' and (result['split'] != 'dev' or not reason.strip() or snapshot_hash is None):
        raise ValueError('AI labels require a development snapshot, fingerprint and explanation')
    payload = dict(grade=grade, origin=origin, reason=reason.strip(), actor=actor,
                   snapshot_hash=result['snapshot_hash'], updated_at=now())
    # Separate keys mean importing AI suggestions can never overwrite a human judgment.
    store.connection.execute('INSERT INTO query_feedback VALUES (?,?,?,?,?) ON CONFLICT(search_id,paper_id,origin) '
        'DO UPDATE SET payload_json=excluded.payload_json,updated_at=excluded.updated_at',
        (identifier, paper_id, origin, json.dumps(payload, ensure_ascii=False), now()))
    return payload
