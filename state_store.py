"""SQLite-backed durable state for CCF Paper Scout."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
import sqlite3
import uuid
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS papers(
  paper_id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', doi TEXT NOT NULL DEFAULT '', first_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recommendation_runs(
  run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
  config_hash TEXT NOT NULL, error_summary TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS recommendation_items(
  run_id TEXT NOT NULL REFERENCES recommendation_runs(run_id) ON DELETE CASCADE,
  paper_id TEXT NOT NULL REFERENCES papers(paper_id), rank INTEGER NOT NULL, score REAL NOT NULL,
  state TEXT NOT NULL, PRIMARY KEY(run_id, paper_id)
);
CREATE TABLE IF NOT EXISTS delivery_attempts(
  attempt_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES recommendation_runs(run_id) ON DELETE CASCADE,
  channel TEXT NOT NULL, status TEXT NOT NULL, provider_message TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS delivery_receipts(
  run_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS translation_cache(
  cache_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, payload_json TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_cursors(
  source TEXT NOT NULL, venue_key TEXT NOT NULL, year INTEGER NOT NULL, next_offset INTEGER NOT NULL,
  last_checked_at TEXT NOT NULL, PRIMARY KEY(source, venue_key, year)
);
CREATE TABLE IF NOT EXISTS candidate_papers(
  paper_id TEXT PRIMARY KEY, venue_key TEXT NOT NULL, year INTEGER NOT NULL,
  payload_json TEXT NOT NULL, text_hash TEXT NOT NULL, first_seen_at TEXT NOT NULL,
  updated_at TEXT NOT NULL, historical_import INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS candidate_partition ON candidate_papers(venue_key, year);
CREATE TABLE IF NOT EXISTS metadata_fetch_state(
  paper_id TEXT PRIMARY KEY, status TEXT NOT NULL, attempts INTEGER NOT NULL,
  next_attempt_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_embeddings(
  cache_key TEXT NOT NULL, text_hash TEXT NOT NULL, model_signature TEXT NOT NULL,
  dimensions INTEGER NOT NULL, vector BLOB NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY(cache_key, model_signature)
);
CREATE TABLE IF NOT EXISTS relevance_cache(
  cache_key TEXT PRIMARY KEY, payload_json TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_aliases(
  namespace TEXT NOT NULL, external_id TEXT NOT NULL, paper_id TEXT NOT NULL REFERENCES papers(paper_id),
  PRIMARY KEY(namespace,external_id,paper_id)
);
CREATE TABLE IF NOT EXISTS paper_titles(
  title_key TEXT NOT NULL, paper_id TEXT NOT NULL REFERENCES papers(paper_id), PRIMARY KEY(title_key,paper_id)
);
CREATE TABLE IF NOT EXISTS source_records(
  source TEXT NOT NULL, record_id TEXT NOT NULL, paper_id TEXT NOT NULL, payload_json TEXT NOT NULL,
  updated_at TEXT NOT NULL, PRIMARY KEY(source,record_id)
);
CREATE TABLE IF NOT EXISTS source_tasks(
  task_key TEXT PRIMARY KEY, source TEXT NOT NULL, state_json TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_health(
  source TEXT PRIMARY KEY, payload_json TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_http_cache(
  cache_key TEXT PRIMARY KEY, source TEXT NOT NULL, payload_json TEXT NOT NULL,
  size_bytes INTEGER NOT NULL, fetched_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS source_runs(
  run_id TEXT NOT NULL, source TEXT NOT NULL, payload_json TEXT NOT NULL,
  PRIMARY KEY(run_id,source)
);
CREATE TABLE IF NOT EXISTS paper_feedback(
  paper_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS search_sessions(
  search_id TEXT PRIMARY KEY, query_key TEXT NOT NULL, split TEXT NOT NULL,
  started_at TEXT NOT NULL, state TEXT NOT NULL, payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS search_query_history ON search_sessions(query_key,started_at);
CREATE TABLE IF NOT EXISTS query_feedback(
  search_id TEXT NOT NULL REFERENCES search_sessions(search_id), paper_id TEXT NOT NULL,
  origin TEXT NOT NULL, payload_json TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY(search_id,paper_id,origin)
);
"""


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def text_hash(paper: dict[str, Any]) -> str:
    text = str(paper.get("title", "")) + "\n" + str(paper.get("abstract", ""))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class StateStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(SCHEMA)
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(source_cursors)")}
        if "state_json" not in columns:
            self.connection.execute("ALTER TABLE source_cursors ADD COLUMN state_json TEXT NOT NULL DEFAULT '{}'")
        self.connection.execute("INSERT OR IGNORE INTO schema_migrations VALUES (3, ?)", (now(),))
        self.connection.execute("INSERT OR IGNORE INTO schema_migrations VALUES (5, ?)", (now(),))
        self.connection.execute("INSERT OR IGNORE INTO schema_migrations VALUES (1, ?)", (now(),))
        self.connection.commit()
        if not self.connection.execute('SELECT 1 FROM schema_migrations WHERE version=4').fetchone():
            from ccf_paper_scout.identity import aliases
            with self.connection:
                for row in self.connection.execute('SELECT paper_id,title,doi FROM papers').fetchall():
                    self._ensure_identity(dict(id=row[0], title=row[1], doi=row[2]))
                    self._save_aliases(row[0], aliases(dict(id=row[0], title=row[1], doi=row[2])))
                for row in self.connection.execute('SELECT payload_json FROM candidate_papers').fetchall():
                    paper = json.loads(row[0])
                    self._ensure_identity(paper)
                    self._save_aliases(paper['id'], aliases(paper))
                self.connection.execute('INSERT INTO schema_migrations VALUES (4,?)', (now(),))

    def close(self) -> None:
        self.connection.close()

    def _ensure_identity(self, paper: dict) -> None:
        from ccf_paper_scout.identity import title
        self.connection.execute('INSERT OR IGNORE INTO papers VALUES (?,?,?,?)',
                                (paper['id'], paper.get('title', ''), paper.get('doi', ''), now()))
        if title(paper.get('title', '')):
            self.connection.execute('INSERT OR IGNORE INTO paper_titles VALUES (?,?)', (title(paper['title']), paper['id']))

    def _save_aliases(self, paper_id: str, aliases) -> None:
        self.connection.executemany('INSERT OR IGNORE INTO paper_aliases VALUES (?,?,?)',
                                    [(kind, value, paper_id) for kind, value in aliases])

    def source_states(self, source: str) -> dict:
        return {r[0]: json.loads(r[1]) for r in self.connection.execute(
            'SELECT task_key,state_json FROM source_tasks WHERE source=?', (source,))}

    def source_health(self, source: str) -> dict:
        row = self.connection.execute('SELECT payload_json FROM source_health WHERE source=?', (source,)).fetchone()
        return json.loads(row[0]) if row else {}

    def http_cache(self, source: str) -> dict:
        return {key: json.loads(raw) for key, raw in self.connection.execute(
            'SELECT cache_key,payload_json FROM source_http_cache WHERE source=?', (source,))}

    def save_http_cache(self, source: str, entry: dict) -> None:
        raw = json.dumps(entry, ensure_ascii=False)
        with self.connection:
            self.connection.execute('INSERT OR REPLACE INTO source_http_cache VALUES (?,?,?,?,?)',
                (entry['cache_key'], source, raw, len(raw.encode('utf-8')), entry['fetched_at']))
            # A small response cache, not an unbounded archive of publisher pages.
            total, count = 0, 0
            for key, size, fetched in self.connection.execute(
                    'SELECT cache_key,size_bytes,fetched_at FROM source_http_cache ORDER BY fetched_at DESC').fetchall():
                total += size
                count += 1
                if total > 64 * 1024 * 1024 or count > 64 or fetched < entry['fetched_at'] - 14 * 86400:
                    self.connection.execute('DELETE FROM source_http_cache WHERE cache_key=?', (key,))

    def save_source_health(self, source: str, run_id: str, status: dict) -> None:
        with self.connection:
            self.connection.execute('INSERT OR REPLACE INTO source_health VALUES (?,?,?)', (source, json.dumps(status), now()))
            self.connection.execute('INSERT OR REPLACE INTO source_runs VALUES (?,?,?)', (run_id, source, json.dumps(status)))

    def save_source_page(self, source: str, page: dict, *, counts=None) -> list[str]:
        """Identity, provenance, candidates and page checkpoint commit together."""
        from ccf_paper_scout import identity, official_abstracts
        saved = []
        changes = dict(new_records=0, abstracts_completed=0)
        with self.connection:
            for record in page['records']:
                paper = dict(record, source=source)
                paper['abstract'] = official_abstracts.usable_abstract(paper.get('abstract', ''))
                external = identity.aliases(paper)
                matches = set()
                for kind, value in external:
                    matches.update(r[0] for r in self.connection.execute(
                        'SELECT paper_id FROM paper_aliases WHERE namespace=? AND external_id=?', (kind, value)))
                source_id = str(paper['source_record_id'])
                identifier = source_id if source == 'dblp' else 'paper:' + str(uuid.uuid5(uuid.NAMESPACE_URL, source + ':' + source_id))
                conflict = len(matches) > 1
                if len(matches) == 1:
                    identifier = next(iter(matches))
                paper['id'] = identifier
                row = self.connection.execute('SELECT payload_json FROM candidate_papers WHERE paper_id=?', (identifier,)).fetchone()
                old = json.loads(row[0]) if row else {}
                if old.get('doi') and paper.get('doi') and identity.doi(old['doi']) != identity.doi(paper['doi']):
                    conflict = True
                # A weak match is a review candidate, never permission to resend under a new ID.
                if not matches:
                    conflict = bool(self.connection.execute('SELECT 1 FROM paper_titles WHERE title_key=? AND paper_id<>? LIMIT 1',
                        (identity.title(paper.get('title')), identifier)).fetchone())
                paper['identity_review_pending'] = conflict
                self._ensure_identity(paper)
                self.connection.execute('INSERT OR REPLACE INTO source_records VALUES (?,?,?,?,?)',
                                        (source, source_id, identifier, json.dumps(paper, ensure_ascii=False), now()))
                if conflict:
                    continue
                self._save_aliases(identifier, external)
                merged = {**old, **{k: v for k, v in paper.items() if v not in (None, '', [])}}
                merged['found_in'] = sorted(set(old.get('found_in', [])) | {source})
                evidence = {json.dumps(e, sort_keys=True): e for e in old.get('publication_evidence', []) + paper.get('publication_evidence', [])}
                merged['publication_evidence'] = list(evidence.values())
                if (old.get('publication_evidence') or old.get('proceedings_id')) and not paper.get('publication_evidence'):
                    for key in ('venue_key', 'venue', 'year', 'proceedings_id', 'rank', 'type', 'url', 'ee', 'publication_status'):
                        if key in old:
                            merged[key] = old[key]
                if official_abstracts.usable_abstract(old.get('abstract', '')):
                    merged['abstract'] = old['abstract']
                    for key in ('abstract_source', 'abstract_source_url', 'abstract_status', 'abstract_method', 'abstract_fetched_at', 'abstract_diagnostic', 'abstract_parser_version'):
                        if key in old:
                            merged[key] = old[key]
                    if paper['abstract'] and official_abstracts.normalized(paper['abstract']) != official_abstracts.normalized(old['abstract']):
                        merged['abstract_diagnostic'] = 'conflicting_sources'
                merged['abstract'] = official_abstracts.usable_abstract(merged.get('abstract', ''))
                if merged['abstract']:
                    merged['abstract_status'] = 'valid'
                changes['new_records'] += not bool(old)
                changes['abstracts_completed'] += bool(old) and not official_abstracts.usable_abstract(old.get('abstract', '')) and bool(merged['abstract'])
                venue, year = merged.get('venue_key', ''), int(merged.get('year') or 0)
                self._upsert_candidate(venue, year, merged, bool(page.get('historical', True)))
                self.connection.execute('UPDATE candidate_papers SET venue_key=?,year=?,payload_json=?,text_hash=? WHERE paper_id=?',
                    (venue, year, json.dumps(merged, ensure_ascii=False), text_hash(merged), identifier))
                saved.append(identifier)
            self.connection.execute('INSERT OR REPLACE INTO source_tasks VALUES (?,?,?,?)',
                                    (page['task_key'], source, json.dumps(page['state']), now()))
        if counts is not None:
            counts.update(changes)
        return saved

    def search_candidates(self, limit: int = 5000) -> list[dict]:
        rows = self.connection.execute('SELECT payload_json FROM candidate_papers ORDER BY updated_at DESC LIMIT ?', (limit,))
        return [json.loads(row[0]) for row in rows]

    def save_feedback(self, paper_id: str, feedback: dict) -> None:
        with self.connection:
            self.connection.execute('INSERT OR REPLACE INTO paper_feedback VALUES (?,?,?)', (paper_id, json.dumps(feedback), now()))

    def start_run(self, config_hash: str) -> str:
        run_id = uuid.uuid4().hex
        self.connection.execute(
            "INSERT INTO recommendation_runs(run_id,started_at,status,config_hash) VALUES (?,?,?,?)",
            (run_id, now(), "running", config_hash),
        )
        self.connection.commit()
        return run_id

    def record_selection(self, run_id: str, papers: list[dict[str, Any]]) -> None:
        with self.connection:
            for rank, paper in enumerate(papers, 1):
                paper_id = str(paper["id"])
                self.connection.execute(
                    "INSERT OR IGNORE INTO papers(paper_id,title,doi,first_seen_at) VALUES (?,?,?,?)",
                    (paper_id, str(paper.get("title", "")), str(paper.get("doi", "")), now()),
                )
                self.connection.execute(
                    "INSERT INTO recommendation_items VALUES (?,?,?,?,?)",
                    (run_id, paper_id, rank, float(paper.get("score", 0)), "selected"),
                )

    def finish_run(self, run_id: str, status: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE recommendation_runs SET status=?, finished_at=? WHERE run_id=?",
                (status, now(), run_id),
            )
            self.connection.execute(
                "UPDATE recommendation_items SET state=? WHERE run_id=?",
                (status, run_id),
            )

    def fail_run(self, run_id: str, message: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE recommendation_runs SET status='failed', finished_at=?, error_summary=? WHERE run_id=?",
                (now(), message, run_id),
            )
            self.connection.execute(
                "UPDATE recommendation_items SET state='failed' WHERE run_id=?", (run_id,)
            )

    def finish_delivery(self, run_id: str, success: bool, message: str = "") -> None:
        status = "delivered" if success else "delivery_failed"
        with self.connection:
            self.connection.execute(
                "INSERT INTO delivery_attempts VALUES (?,?,?,?,?,?)",
                (uuid.uuid4().hex, run_id, "smtp", status, message, now()),
            )
            self.connection.execute(
                "UPDATE recommendation_runs SET status=?, finished_at=?, error_summary=? WHERE run_id=?",
                (status, now(), "" if success else message, run_id),
            )
            self.connection.execute(
                "UPDATE recommendation_items SET state=? WHERE run_id=?",
                (status, run_id),
            )

    def delivered_ids(self) -> set[str]:
        rows = self.connection.execute(
            "SELECT DISTINCT paper_id FROM recommendation_items WHERE state='delivered'"
        )
        result = {row[0] for row in rows}
        for row in self.connection.execute('SELECT payload_json FROM delivery_receipts'):
            result.update(json.loads(row[0])['selected_paper_ids'])
        return result

    def reconcile_receipts(self, receipts) -> None:
        from ccf_paper_scout.delivery_receipts import validate
        with self.connection:
            for receipt in receipts:
                validate(receipt)
                previous = self.connection.execute('SELECT payload_json FROM delivery_receipts WHERE run_id=?',
                                                   (receipt['run_id'],)).fetchone()
                if previous and json.loads(previous[0]) != receipt:
                    raise ValueError('conflicting persisted delivery receipt')
                for identifier in receipt['selected_paper_ids']:
                    self._ensure_identity(dict(id=identifier))
                    self._save_aliases(identifier, receipt.get('paper_aliases', {}).get(identifier, []))
                self.connection.execute('INSERT OR IGNORE INTO delivery_receipts VALUES (?,?)',
                                        (receipt['run_id'], json.dumps(receipt, sort_keys=True)))

    def save_translation(self, key: str, fingerprint: str, payload: dict[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO translation_cache VALUES (?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET fingerprint=excluded.fingerprint,payload_json=excluded.payload_json,updated_at=excluded.updated_at",
            (key, fingerprint, json.dumps(payload, ensure_ascii=False), now()),
        )
        self.connection.commit()

    def load_translation(self, key: str, fingerprint: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT payload_json FROM translation_cache WHERE cache_key=? AND fingerprint=?", (key, fingerprint)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def save_cursor(self, source: str, venue_key: str, year: int, next_offset: int) -> None:
        self.connection.execute(
            "INSERT INTO source_cursors(source,venue_key,year,next_offset,last_checked_at) VALUES (?,?,?,?,?) ON CONFLICT(source,venue_key,year) DO UPDATE SET next_offset=excluded.next_offset,last_checked_at=excluded.last_checked_at",
            (source, venue_key, year, next_offset, now()),
        )
        self.connection.commit()

    def load_cursor(self, source: str, venue_key: str, year: int) -> int:
        row = self.connection.execute(
            "SELECT next_offset FROM source_cursors WHERE source=? AND venue_key=? AND year=?",
            (source, venue_key, year),
        ).fetchone()
        return int(row[0]) if row else 0

    def scan_state(self, venue_key: str, year: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT next_offset,last_checked_at,state_json FROM source_cursors WHERE source='DBLP' AND venue_key=? AND year=?",
            (venue_key, year),
        ).fetchone()
        if not row:
            return {}
        return {**json.loads(row[2]), "next_offset": row[0], "last_checked_at": row[1]}

    def save_discovery_page(self, venue_key: str, year: int, papers: list[dict[str, Any]], state: dict[str, Any]) -> int:
        """Commit page contents and cursor together; a failed transaction cannot skip a page."""
        added = 0
        with self.connection:
            for paper in papers:
                added += self._upsert_candidate(venue_key, year, paper, not state.get("initialized", False))
            self.connection.execute(
                "INSERT INTO source_cursors(source,venue_key,year,next_offset,last_checked_at,state_json) VALUES ('DBLP',?,?,?,?,?) "
                "ON CONFLICT(source,venue_key,year) DO UPDATE SET next_offset=excluded.next_offset,last_checked_at=excluded.last_checked_at,state_json=excluded.state_json",
                (venue_key, year, state.get("next_offset", 0), now(), json.dumps(state)),
            )
        return added

    def _upsert_candidate(self, venue_key: str, year: int, paper: dict[str, Any], historical: bool) -> int:
        row = self.connection.execute("SELECT payload_json FROM candidate_papers WHERE paper_id=?", (paper["id"],)).fetchone()
        payload = json.loads(row[0]) if row else {}
        # A source refresh without an abstract must not erase enriched metadata.
        payload.update({key: value for key, value in paper.items() if value not in (None, "", [])})
        self.connection.execute(
            "INSERT INTO candidate_papers VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(paper_id) DO UPDATE SET "
            "payload_json=excluded.payload_json,text_hash=excluded.text_hash,updated_at=excluded.updated_at",
            (paper["id"], venue_key, year, json.dumps(payload, ensure_ascii=False), text_hash(payload), now(), now(), int(historical)),
        )
        from ccf_paper_scout.identity import aliases
        self._ensure_identity(paper)
        self._save_aliases(paper['id'], aliases(paper))
        return int(row is None)

    def candidates(self, venue_keys: list[str], years: list[int]) -> list[dict[str, Any]]:
        if not venue_keys or not years:
            return []
        rows = self.connection.execute(
            f"SELECT payload_json,first_seen_at,historical_import FROM candidate_papers WHERE venue_key IN ({','.join('?' for _ in venue_keys)}) AND year IN ({','.join('?' for _ in years)})",
            [*venue_keys, *years],
        )
        return [{**json.loads(row[0]), "first_seen_at": row[1], "historical_import": bool(row[2])} for row in rows]

    def update_candidates(self, papers: list[dict[str, Any]]) -> None:
        """Persist qualification/evidence without changing delivery or metadata retry state."""
        with self.connection:
            self.connection.executemany(
                "UPDATE candidate_papers SET payload_json=?,text_hash=?,updated_at=? WHERE paper_id=?",
                [(json.dumps(paper, ensure_ascii=False), text_hash(paper), now(), paper["id"]) for paper in papers],
            )

    def metadata_due(self, paper_id: str) -> bool:
        row = self.connection.execute("SELECT next_attempt_at FROM metadata_fetch_state WHERE paper_id=?", (paper_id,)).fetchone()
        return row is None or row[0] <= now()

    def save_metadata(self, paper: dict[str, Any], status: str) -> None:
        row = self.connection.execute("SELECT attempts FROM metadata_fetch_state WHERE paper_id=?", (paper["id"],)).fetchone()
        attempts = (row[0] if row else 0) + 1
        days = 30 if status == "success" else 7 if status in ("missing", "no_doi") else min(7, 2 ** min(attempts - 1, 3))
        retry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)).isoformat()
        with self.connection:
            self.connection.execute(
                "UPDATE candidate_papers SET payload_json=?,text_hash=?,updated_at=? WHERE paper_id=?",
                (json.dumps(paper, ensure_ascii=False), text_hash(paper), now(), paper["id"]),
            )
            self.connection.execute(
                "INSERT INTO metadata_fetch_state VALUES (?,?,?,?,?) ON CONFLICT(paper_id) DO UPDATE SET "
                "status=excluded.status,attempts=excluded.attempts,next_attempt_at=excluded.next_attempt_at,updated_at=excluded.updated_at",
                (paper["id"], status, attempts, retry, now()),
            )

    def load_vector(self, key: str, fingerprint: str, signature: str) -> bytes | None:
        row = self.connection.execute(
            "SELECT vector FROM paper_embeddings WHERE cache_key=? AND text_hash=? AND model_signature=?",
            (key, fingerprint, signature),
        ).fetchone()
        return bytes(row[0]) if row else None

    def save_vector(self, key: str, fingerprint: str, signature: str, dimensions: int, vector: bytes) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO paper_embeddings VALUES (?,?,?,?,?,?) ON CONFLICT(cache_key,model_signature) DO UPDATE SET "
                "text_hash=excluded.text_hash,dimensions=excluded.dimensions,vector=excluded.vector,updated_at=excluded.updated_at",
                (key, fingerprint, signature, dimensions, vector, now()),
            )

    def load_review(self, key: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT payload_json FROM relevance_cache WHERE cache_key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_review(self, key: str, payload: dict[str, Any]) -> None:
        with self.connection:
            self.connection.execute("INSERT OR REPLACE INTO relevance_cache VALUES (?,?,?)", (key, json.dumps(payload), now()))

    def migrate_legacy(self, seen_path: Path, translations_path: Path) -> None:
        marker = self.connection.execute("SELECT 1 FROM schema_migrations WHERE version=2").fetchone()
        if marker:
            return
        seen = json.loads(seen_path.read_text(encoding="utf-8")) if seen_path.exists() else {"ids": []}
        translations = json.loads(translations_path.read_text(encoding="utf-8")) if translations_path.exists() else {}
        stamp = dt.datetime.now().strftime("%Y%m%d%H%M%S")
        backups: list[tuple[Path, Path]] = []
        for path in (seen_path, translations_path):
            if path.exists():
                backup = path.with_name(path.name + f".{stamp}.{uuid.uuid4().hex[:8]}.bak")
                shutil.copy2(path, backup)
                backups.append((path, backup))
        run_id = "legacy-import"
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO recommendation_runs(run_id,started_at,finished_at,status,config_hash) VALUES (?,?,?,?,?)",
                (run_id, now(), now(), "delivered", "legacy"),
            )
            for rank, paper_id in enumerate(seen.get("ids", []), 1):
                self.connection.execute("INSERT OR IGNORE INTO papers VALUES (?,?,?,?)", (paper_id, "", "", now()))
                from ccf_paper_scout.identity import aliases
                self._save_aliases(paper_id, aliases({'id': paper_id}))
                self.connection.execute(
                    "INSERT OR IGNORE INTO recommendation_items VALUES (?,?,?,?,?)", (run_id, paper_id, rank, 0, "delivered")
                )
            for key, payload in translations.items():
                fingerprint = str(payload.get("_fingerprint", "legacy"))
                self.connection.execute(
                    "INSERT OR IGNORE INTO translation_cache VALUES (?,?,?,?)",
                    (key, fingerprint, json.dumps(payload, ensure_ascii=False), now()),
                )
            self.connection.execute("INSERT INTO schema_migrations VALUES (2, ?)", (now(),))
