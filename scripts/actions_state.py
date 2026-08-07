#!/usr/bin/env python3
"""Prepare, verify and checkpoint the allowlisted Actions runtime state."""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

ALLOWED = {"paper_scout.sqlite3", "seen.json", "translations.json", ".delivery-pending", ".gitignore", "README.md"}
RUNTIME_SUFFIXES = (".lock", ".sqlite3-wal", ".sqlite3-shm")


def prepare(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for path in root.iterdir():
        if path.is_file() and (path.name.endswith(RUNTIME_SUFFIXES) or path.name.endswith(".bak")):
            path.unlink()


def verify(root: Path) -> None:
    unexpected = sorted(p.name for p in root.iterdir() if p.name not in ALLOWED)
    if unexpected:
        raise RuntimeError("unexpected state entries: " + ", ".join(unexpected))
    db = root / "paper_scout.sqlite3"
    if db.exists():
        connection = sqlite3.connect(db)
        try:
            result = connection.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            connection.close()
        if result != "ok":
            raise RuntimeError("SQLite integrity check failed")


def checkpoint(root: Path) -> None:
    db = root / "paper_scout.sqlite3"
    if db.exists():
        connection = sqlite3.connect(db)
        try:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            result = connection.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            connection.close()
        if result != "ok":
            raise RuntimeError("SQLite integrity check failed")
    prepare(root)
    verify(root)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare", "verify", "checkpoint"))
    parser.add_argument("--dir", type=Path, required=True)
    args = parser.parse_args()
    globals()[args.command](args.dir)
    print(f"Actions state {args.command} completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
