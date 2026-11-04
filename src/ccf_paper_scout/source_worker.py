"""One isolated source process. Only its parent owns the database."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

from .source_adapters import HOSTS, collect, accumulate_diagnostics
from .source_http import Runtime, SourceError


def write(path, payload):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)


def run(task, directory: Path):
    runtime = Runtime(task['settings'], HOSTS[task['source']], cache=task.get('http_cache', {}),
        save_cache=lambda entry: write(directory / ('cache-' + str(time.time_ns()) + '.json'), entry),
        progress=lambda values: write(directory / 'progress.json', values))
    started = time.monotonic()
    status = dict(status='empty', code='', records=0, pages=0, failures={}, discovered=0)
    try:
        for page in collect(task, runtime):
            write(directory / f"page-{status['pages']:05d}.json", page)
            status['pages'] += 1
            status['records'] += len(page['records'])
            diagnostics = page.get('diagnostics', {})
            accumulate_diagnostics(status, diagnostics)
            status['status'] = 'partial' if status['failures'] or status.get('deferred') else 'ok' if status['records'] else 'empty'
    except SourceError as exc:
        status.update(status=exc.status, code=exc.code, retry_after=exc.retry_after)
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        status.update(status='protocol_error', code=type(exc).__name__)
    finally:
        status.update(requests=runtime.requests, bytes=runtime.bytes, cache_hits=runtime.cache_hits,
                      seconds=round(time.monotonic()-started, 3), quota=runtime.quota)
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
