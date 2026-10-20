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
        self.connection.execute("INSERT OR IGNORE INTO schema_migrations VALUES (1, ?)", (now(),))
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

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
        return {row[0] for row in rows}

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
        return int(row is None)

    def candidates(self, venue_keys: list[str], years: list[int]) -> list[dict[str, Any]]:
        if not venue_keys or not years:
            return []
        rows = self.connection.execute(
            f"SELECT payload_json,first_seen_at,historical_import FROM candidate_papers WHERE venue_key IN ({','.join('?' for _ in venue_keys)}) AND year IN ({','.join('?' for _ in years)})",
            [*venue_keys, *years],
        )
        return [{**json.loads(row[0]), "first_seen_at": row[1], "historical_import": bool(row[2])} for row in rows]

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
