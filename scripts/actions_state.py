#!/usr/bin/env python3
"""Prepare, verify and checkpoint the allowlisted Actions runtime state."""
from __future__ import annotations

import argparse
import datetime as dt
import base64
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

from ccf_paper_scout import delivery_receipts

ALLOWED = {"paper_scout.sqlite3", "paper_scout.sqlite3.gz", "snapshot.json", "seen.json", "translations.json", "calibration.json", "relation-calibration.json", "evaluation-labels.json", ".delivery-pending", "delivery-receipts.jsonl", "delivery-recovery-audit.jsonl", ".gitignore", "README.md"}
RUNTIME_SUFFIXES = (".lock", ".sqlite3-wal", ".sqlite3-shm")
DB_NAME = 'paper_scout.sqlite3'
WARN_BYTES = 50 * 1024 * 1024
MAX_BYTES = 90 * 1024 * 1024
MAX_RAW_BYTES = 2 * 1024 * 1024 * 1024
SMALL_FILES = ('seen.json', delivery_receipts.FILENAME, '.delivery-pending', 'delivery-recovery-audit.jsonl')


def prepare(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    # Never delete a WAL: it can contain committed delivery or candidate records.


def verify(root: Path) -> None:
    unexpected = []
    for path in root.iterdir():
        if (path.name not in ALLOWED and not path.name.endswith(RUNTIME_SUFFIXES)) or path.is_symlink() or not path.is_file():
            unexpected.append(path.name)
    unexpected.sort()
    if unexpected:
        raise RuntimeError("unexpected state entries: " + ", ".join(unexpected))
    db = root / "paper_scout.sqlite3"
    if db.exists():
        connection = sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True)
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
            result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if result[0]:
                raise RuntimeError('SQLite checkpoint is busy; retain WAL and retry after writers stop')
            result = connection.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            connection.close()
        if result != "ok":
            raise RuntimeError("SQLite integrity check failed")
    prepare(root)
    verify(root)


def digest(path: Path) -> str:
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def reconcile(root: Path) -> None:
    records = delivery_receipts.reconcile(root)
    if records:
        from state_store import StateStore
        store = StateStore(root / DB_NAME)
        try:
            store.reconcile_receipts(records.values())
        finally:
            store.close()


def restore(root: Path) -> None:
    prepare(root)
    verify(root)
    archive, manifest, db = root / (DB_NAME + '.gz'), root / 'snapshot.json', root / DB_NAME
    if archive.exists() or manifest.exists():
        if db.exists() or not archive.exists() or not manifest.exists():
            raise RuntimeError('ambiguous or incomplete state snapshot; refusing empty/legacy fallback')
        info = json.loads(manifest.read_text(encoding='utf-8'))
        if (info.get('version') != 1 or type(info.get('raw_bytes')) is not int or
                not 0 < info['raw_bytes'] <= MAX_RAW_BYTES or type(info.get('gzip_bytes')) is not int or
                not 0 < info['gzip_bytes'] < MAX_BYTES):
            raise RuntimeError('unsupported or oversized snapshot')
        if archive.stat().st_size != info['gzip_bytes'] or digest(archive) != info['gzip_sha256']:
            raise RuntimeError('compressed snapshot checksum mismatch')
        with tempfile.TemporaryDirectory(prefix='scout-restore-', dir=root.parent) as tmp:
            extracted = Path(tmp) / DB_NAME
            size = 0
            with gzip.open(archive, 'rb') as src, extracted.open('wb') as dest:
                while chunk := src.read(1024 * 1024):
                    size += len(chunk)
                    if size > info['raw_bytes']:
                        raise RuntimeError('snapshot exceeds declared uncompressed size')
                    dest.write(chunk)
            if size != info['raw_bytes'] or digest(extracted) != info['raw_sha256']:
                raise RuntimeError('uncompressed snapshot checksum mismatch')
            verify(Path(tmp))
            extracted.replace(db)
    elif not db.exists():
        # A README-only initialized repository is a supported first deployment.
        if any(path.name not in ('.gitignore', 'README.md') for path in root.iterdir()):
            raise RuntimeError('database missing from existing state; refusing empty initialization')
        from state_store import StateStore
        store = StateStore(db)
        store.close()
    reconcile(root)
    verify(root)


def pack(root: Path, *, check_only: bool = False) -> dict:
    """Back up committed WAL content, then verify a deterministic gzip snapshot."""
    verify(root)
    db = root / DB_NAME
    if not db.exists():
        raise RuntimeError('cannot pack a missing database')
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='scout-pack-', dir=root.parent) as tmp:
        snapshot = Path(tmp) / DB_NAME
        source = sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True)
        target = sqlite3.connect(snapshot)
        try:
            def progress(status, remaining, total):
                if time.monotonic() - started > 120:
                    raise RuntimeError('database backup timed out; stop writers before retrying')
            source.backup(target, pages=1024, progress=progress)
            if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('snapshot SQLite integrity check failed')
        finally:
            target.close()
            source.close()
        if snapshot.stat().st_size > MAX_RAW_BYTES:
            raise RuntimeError('database exceeds runtime size limit')
        compressed = Path(tmp) / (DB_NAME + '.gz')
        with snapshot.open('rb') as src, compressed.open('wb') as raw:
            with gzip.GzipFile(filename='', mode='wb', fileobj=raw, compresslevel=6, mtime=0) as dest:
                shutil.copyfileobj(src, dest)
        info = dict(version=1, raw_bytes=snapshot.stat().st_size, gzip_bytes=compressed.stat().st_size,
                    raw_sha256=digest(snapshot), gzip_sha256=digest(compressed))
        with gzip.open(compressed, 'rb') as handle:
            if hashlib.file_digest(handle, 'sha256').hexdigest() != info['raw_sha256']:
                raise RuntimeError('snapshot compression roundtrip mismatch')
        if info['gzip_bytes'] >= MAX_BYTES:
            raise RuntimeError('compressed database exceeds 90 MiB upload budget')
        if info['gzip_bytes'] >= WARN_BYTES:
            print('::warning::Compressed database is at least 50 MiB; review cache retention')
        if not check_only:
            compressed.replace(root / compressed.name)
            delivery_receipts.atomic_json(root / 'snapshot.json', info)
    print(json.dumps(dict(snapshot=info, seconds=round(time.monotonic()-started, 3), check_only=check_only)))
    return info


def git(repo: Path, *args: str, capture: bool = False) -> str:
    env = os.environ.copy()
    token = env.get('STATE_REPO_TOKEN')
    if args[0] == 'push' and token:
        index = int(env.get('GIT_CONFIG_COUNT', '0'))
        env.update({'GIT_CONFIG_COUNT': str(index + 1), f'GIT_CONFIG_KEY_{index}': 'http.https://github.com/.extraheader',
                    f'GIT_CONFIG_VALUE_{index}': 'AUTHORIZATION: basic ' + base64.b64encode(('x-access-token:' + token).encode()).decode()})
    result = subprocess.run(['git', '-C', str(repo), *args], env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
    if not capture:
        print(result.stdout, end='')
    if result.returncode:
        # Do not expose environment credentials in exception command arguments.
        print(result.stderr, end='')
        raise RuntimeError('state git operation failed: ' + args[0])
    return result.stdout


def commit_state(root: Path, names, message: str) -> None:
    repo = root.parent
    if git(repo, 'diff', '--cached', '--name-only', capture=True).strip():
        raise RuntimeError('unexpected staged files before state transaction')
    tracked = set(git(repo, 'ls-files', '--', root.name, capture=True).splitlines())
    paths = [root.name + '/' + name for name in names if (root / name).exists() or root.name + '/' + name in tracked]
    for path in paths:
        f = repo / path
        if f.exists() and f.stat().st_size >= MAX_BYTES:
            raise RuntimeError('state file exceeds upload budget: ' + f.name)
    if paths:
        git(repo, 'add', '-A', '--', *paths)
    if git(repo, 'diff', '--cached', '--name-only', capture=True).strip():
        git(repo, 'commit', '-m', message)
    # Push even if no diff: a previous attempt may have committed but not pushed.
    git(repo, 'push', 'origin', 'HEAD:main')


def persist(root: Path) -> None:
    repo = root.parent
    git(repo, 'config', 'user.name', 'github-actions[bot]')
    git(repo, 'config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com')
    delivery_receipts.reconcile(root)
    commit_state(root, SMALL_FILES, 'state: delivery receipts ' + os.environ.get('GITHUB_RUN_ID', 'local'))
    # Large snapshot failures cannot undo the preceding independent remote push.
    reconcile(root)
    checkpoint(root)
    pack(root)
    tracked = git(repo, 'ls-files', '--', root.name + '/' + DB_NAME, capture=True).strip()
    if tracked:
        git(repo, 'rm', '--cached', '--', root.name + '/' + DB_NAME)
    ignore = root / '.gitignore'
    lines = ignore.read_text(encoding='utf-8').splitlines() if ignore.exists() else []
    for pattern in (DB_NAME, '*.sqlite3-wal', '*.sqlite3-shm', '*.lock', '.delivery-approved', '*.tmp'):
        if pattern not in lines:
            lines.append(pattern)
    ignore.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    # Migration's staged removal is deliberate; other files are explicitly selected.
    names = sorted(ALLOWED - set(SMALL_FILES) - {DB_NAME})
    paths = [root.name + '/' + name for name in names if (root / name).exists()]
    for path in paths:
        if (repo / path).stat().st_size >= MAX_BYTES:
            raise RuntimeError('state file exceeds upload budget')
    git(repo, 'add', '--', *paths)
    if git(repo, 'diff', '--cached', '--name-only', capture=True).strip():
        git(repo, 'commit', '-m', 'state: compressed snapshot ' + os.environ.get('GITHUB_RUN_ID', 'local'))
    git(repo, 'push', 'origin', 'HEAD:main')


def persist_pending(root: Path) -> None:
    if delivery_status(root).get('phase') != 'delivery_pending':
        raise RuntimeError('expected pending transaction before SMTP')
    git(root.parent, 'config', 'user.name', 'github-actions[bot]')
    git(root.parent, 'config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com')
    commit_state(root, ['.delivery-pending'], 'state: pending delivery ' + os.environ.get('GITHUB_RUN_ID', 'local'))


def stats(root: Path, config: dict | None = None) -> dict:
    result = {'files': {p.name: p.stat().st_size for p in root.iterdir() if p.is_file() and p.name in ALLOWED}}
    if (root / DB_NAME).exists():
        con = sqlite3.connect((root / DB_NAME).resolve().as_uri() + '?mode=ro', uri=True)
        try:
            result['rows'] = {name: con.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
                              for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            result['free_bytes'] = con.execute('PRAGMA freelist_count').fetchone()[0] * con.execute('PRAGMA page_size').fetchone()[0]
            try:
                result['allocated_bytes'] = dict(con.execute('SELECT name,sum(pgsize) FROM dbstat GROUP BY name'))
                result['size_method'] = 'SQLite pages including indexes'
            except sqlite3.OperationalError:
                result['field_bytes'] = {}
                result['size_method'] = 'field bytes only; excludes indexes and page overhead'
                for name in result['rows']:
                    quoted = '"' + name.replace('"', '""') + '"'
                    columns = [r[1] for r in con.execute('PRAGMA table_info(' + quoted + ')')]
                    expression = '+'.join('coalesce(length(cast("' + c.replace('"', '""') + '" as blob)),0)' for c in columns)
                    result['field_bytes'][name] = con.execute('SELECT coalesce(sum(' + expression + '),0) FROM ' + quoted).fetchone()[0]
            if 'candidate_papers' in result['rows']:
                from ccf_paper_scout import official_abstracts
                from ccf_paper_scout.discovery import resolve_years
                active_years = resolve_years(config) if config is not None else []
                counts = dict(total=0, nonempty_abstracts=0, valid_abstracts=0, active=0,
                              active_valid_abstracts=0, active_stored_verified=0)
                for venue, year, raw in con.execute('SELECT venue_key,year,payload_json FROM candidate_papers'):
                    paper = json.loads(raw)
                    valid = bool(official_abstracts.usable_abstract(paper.get('abstract', '')))
                    counts['total'] += 1
                    counts['nonempty_abstracts'] += bool(paper.get('abstract'))
                    counts['valid_abstracts'] += valid
                    if config is not None and venue in config['venue_keys'] and year in active_years:
                        counts['active'] += 1
                        counts['active_valid_abstracts'] += valid
                        counts['active_stored_verified'] += paper.get('main_conference_status') == 'verified'
                result['candidates'] = counts
                result['active_scope_configured'] = config is not None
                result['active_years'] = active_years
                result['qualification_note'] = 'stored qualification, not a new eligibility audit'
            if 'source_http_cache' in result['rows']:
                result['http_cache_payload_bytes'] = dict(con.execute('SELECT source,sum(size_bytes) FROM source_http_cache GROUP BY source'))
            if 'paper_embeddings' in result['rows']:
                result['vectors_by_signature'] = dict(con.execute('SELECT model_signature,count(*) FROM paper_embeddings GROUP BY model_signature'))
        finally:
            con.close()
    print(json.dumps(result, sort_keys=True))
    return result


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
    parser.add_argument("command", choices=("prepare", "restore", "verify", "checkpoint", "pack", "persist", "persist-pending", "stats", "reconcile-delivery", "delivery-status", "clear-pending"))
    parser.add_argument("--dir", type=Path, required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--reason")
    parser.add_argument('--check-only', action='store_true')
    parser.add_argument('--config', type=Path, help='optional config for active venue/year statistics')
    args = parser.parse_args()
    command = args.command.replace("-", "_")
    if command == "delivery_status":
        for key, value in delivery_status(args.dir).items():
            print(f"{key}: {value}")
    elif command == "clear_pending":
        if not args.run_id or not args.reason:
            parser.error("clear-pending requires --run-id and --reason")
        clear_pending(args.dir, args.run_id, args.reason)
    elif command == 'pack':
        pack(args.dir, check_only=args.check_only)
    elif command == 'reconcile_delivery':
        reconcile(args.dir)
    elif command == 'stats':
        if args.config:
            from ccf_paper_scout.cli import load_config
            stats(args.dir, load_config(args.config))
        else:
            stats(args.dir)
    else:
        globals()[command](args.dir)
    print(f"Actions state {args.command} completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
