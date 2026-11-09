#!/usr/bin/env python3
"""Inspect cache retention or create a cleaned database copy; never overwrite the input."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sqlite3
import time


def cleanup_plan(con, *, http_bytes=16 * 1024 * 1024, max_age_days=14, keep_signatures=(), now=None):
    if type(http_bytes) is not int or http_bytes < 0 or not math.isfinite(max_age_days) or max_age_days < 0:
        raise ValueError('cache budgets must be nonnegative and finite')
    now = time.time() if now is None else now
    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    expired, over_budget, retained = [], [], 0
    if 'source_http_cache' in tables:
        for key, size, fetched in con.execute('SELECT cache_key,size_bytes,fetched_at FROM source_http_cache ORDER BY fetched_at DESC,cache_key'):
            if fetched < now - max_age_days * 86400:
                expired.append((key, size))
            elif retained + size > http_bytes:
                over_budget.append((key, size))
            else:
                retained += size
    signatures = {}
    if 'paper_embeddings' in tables:
        signatures = {row[0]: row[1] for row in con.execute('SELECT model_signature,count(*) FROM paper_embeddings GROUP BY model_signature')}
    if keep_signatures and not set(keep_signatures) <= signatures.keys():
        raise ValueError('keep-model-signature must name an existing signature; refusing a likely typo')
    discarded = sorted(set(signatures) - set(keep_signatures)) if keep_signatures else []
    return dict(http_keys=[key for key, _ in expired + over_budget], model_signatures=discarded,
                summary=dict(expired_http_records=len(expired), over_budget_http_records=len(over_budget),
                             removed_http_payload_bytes=sum(size for _, size in expired + over_budget),
                             retained_http_payload_bytes=retained,
                             removed_vectors=sum(signatures[k] for k in discarded),
                             vector_signatures=signatures, candidates_removed=0))


def maintain(database: Path, output: Path | None = None, **settings):
    database = database.resolve(strict=True)
    if output is not None and (output.resolve() == database or output.exists()):
        raise ValueError('output must be a new file, separate from the source database')
    source = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
    try:
        if output is None:
            source.execute('BEGIN')
            result = cleanup_plan(source, **settings)['summary']
            return dict(result, dry_run=True, database_bytes=database.stat().st_size)
        output.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(output)
        try:
            source.backup(target)
            plan = cleanup_plan(target, **settings)
            with target:
                if plan['http_keys']:
                    target.executemany('DELETE FROM source_http_cache WHERE cache_key=?', [(k,) for k in plan['http_keys']])
                if plan['model_signatures']:
                    target.executemany('DELETE FROM paper_embeddings WHERE model_signature=?', [(s,) for s in plan['model_signatures']])
            # This is an isolated copy: compact it after writes have ended.
            target.execute('PRAGMA journal_mode=DELETE')
            target.execute('VACUUM')
            if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('cleaned copy failed SQLite integrity check')
            return dict(plan['summary'], dry_run=False, database_bytes=database.stat().st_size,
                        output_bytes=output.stat().st_size)
        finally:
            target.close()
    finally:
        source.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--output', type=Path, help='new cleaned copy; omit for a read-only retention preview')
    parser.add_argument('--http-cache-mib', type=int, default=16)
    parser.add_argument('--max-age-days', type=float, default=14)
    parser.add_argument('--keep-model-signature', action='append', default=[])
    args = parser.parse_args()
    print(json.dumps(maintain(args.database, args.output, http_bytes=args.http_cache_mib * 1024 * 1024,
                              max_age_days=args.max_age_days, keep_signatures=args.keep_model_signature), sort_keys=True))


if __name__ == '__main__':
    main()
