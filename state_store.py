"""SQLite-backed durable state for CCF Paper Scout."""
from __future__ import annotations

import datetime as dt
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
"""


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


class StateStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(SCHEMA)
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
            "INSERT INTO source_cursors VALUES (?,?,?,?,?) ON CONFLICT(source,venue_key,year) DO UPDATE SET next_offset=excluded.next_offset,last_checked_at=excluded.last_checked_at",
            (source, venue_key, year, next_offset, now()),
        )
        self.connection.commit()

    def load_cursor(self, source: str, venue_key: str, year: int) -> int:
        row = self.connection.execute(
            "SELECT next_offset FROM source_cursors WHERE source=? AND venue_key=? AND year=?",
            (source, venue_key, year),
        ).fetchone()
        return int(row[0]) if row else 0

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
