#!/usr/bin/env python3
"""Prepare, verify and checkpoint the allowlisted Actions runtime state."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
from pathlib import Path

ALLOWED = {"paper_scout.sqlite3", "seen.json", "translations.json", ".delivery-pending", "delivery-recovery-audit.jsonl", ".gitignore", "README.md"}
RUNTIME_SUFFIXES = (".lock", ".sqlite3-wal", ".sqlite3-shm")


def prepare(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for path in root.iterdir():
        if path.is_file() and (path.name.endswith(RUNTIME_SUFFIXES) or path.name.endswith(".bak")):
            path.unlink()


def verify(root: Path) -> None:
    unexpected = []
    for path in root.iterdir():
        if path.name not in ALLOWED or path.is_symlink() or not path.is_file():
            unexpected.append(path.name)
    unexpected.sort()
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


def delivery_status(root: Path) -> dict[str, object]:
    marker = root / ".delivery-pending"
    if not marker.exists():
        return {"phase": "none", "selected_papers": 0}
    text = marker.read_text(encoding="utf-8")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
        payload["phase"] = "legacy_pending"
    payload["selected_papers"] = len(payload.get("selected_paper_ids", []))
    return payload


def clear_pending(root: Path, run_id: str, reason: str) -> None:
    marker = root / ".delivery-pending"
    status = delivery_status(root)
    if status.get("phase") == "none":
        raise RuntimeError("no pending delivery marker exists")
    if str(status.get("run_id", "")) != run_id:
        raise RuntimeError("run_id does not match pending marker")
    if status.get("phase") == "smtp_accepted":
        raise RuntimeError("refusing to clear smtp_accepted delivery marker")
    audit = root / "delivery-recovery-audit.jsonl"
    entry = {"cleared_at": dt.datetime.now(dt.timezone.utc).isoformat(), "run_id": run_id, "phase": status.get("phase"), "reason": reason}
    with audit.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    marker.unlink()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare", "verify", "checkpoint", "delivery-status", "clear-pending"))
    parser.add_argument("--dir", type=Path, required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--reason")
    args = parser.parse_args()
    command = args.command.replace("-", "_")
    if command == "delivery_status":
        for key, value in delivery_status(args.dir).items():
            print(f"{key}: {value}")
    elif command == "clear_pending":
        if not args.run_id or not args.reason:
            parser.error("clear-pending requires --run-id and --reason")
        clear_pending(args.dir, args.run_id, args.reason)
    else:
        globals()[command](args.dir)
    print(f"Actions state {args.command} completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
