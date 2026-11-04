"""One isolated source process. Only its parent owns the database."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

from .source_adapters import HOSTS, collect
from .source_http import Runtime, SourceError


def write(path, payload):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)


def run(task, directory: Path):
    runtime = Runtime(task['settings'], HOSTS[task['source']])
    started = time.monotonic()
    status = dict(status='empty', code='', records=0, pages=0)
    try:
        for page in collect(task, runtime):
            write(directory / f"page-{status['pages']:05d}.json", page)
            status['pages'] += 1
            status['records'] += len(page['records'])
            status['status'] = 'ok' if status['records'] else 'empty'
    except SourceError as exc:
        status.update(status=exc.status, code=exc.code, retry_after=exc.retry_after)
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        status.update(status='protocol_error', code=type(exc).__name__)
    finally:
        status.update(requests=runtime.requests, bytes=runtime.bytes, seconds=round(time.monotonic()-started, 3), quota=runtime.quota)
        runtime.close()
        write(directory / 'result.json', status)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--task', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    run(json.loads(args.task.read_text(encoding='utf-8')), args.output)


if __name__ == '__main__':
    main()
