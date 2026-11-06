"""Multi-source scheduling with hard deadlines, durable page commits and health state."""
from __future__ import annotations

import datetime as dt
import copy
import json
import math
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

from .source_adapters import HOSTS, OFFICIAL, key, accumulate_diagnostics
from . import identity, official_abstracts

DEFAULTS = {
    'dblp': dict(max_requests=24, deadline_seconds=180, min_interval_seconds=3, read_timeout_seconds=30),
    'openalex': dict(max_requests=8, deadline_seconds=90, min_interval_seconds=1),
    'arxiv': dict(max_requests=2, deadline_seconds=60, min_interval_seconds=4),
    'semantic_scholar': dict(max_requests=8, deadline_seconds=90, min_interval_seconds=1.2),
    'crossref': dict(max_requests=10, deadline_seconds=60, min_interval_seconds=1),
    'openreview': dict(max_requests=8, deadline_seconds=90, min_interval_seconds=1),
}


def validate(settings):
    enabled = settings.get('enabled', [])
    if not isinstance(enabled, list) or any(s not in HOSTS for s in enabled) or len(set(enabled)) != len(enabled):
        raise ValueError('sources.enabled must contain unique supported source names')
    for name, value in [('max_parallel_sources', settings.get('max_parallel_sources', 2)),
                        ('collection_deadline_seconds', settings.get('collection_deadline_seconds', 900))]:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f'invalid sources.{name}')
    if settings.get('mode', 'shadow') not in ('shadow', 'production'):
        raise ValueError('sources.mode must be shadow or production')
    if type(settings.get('max_parallel_sources', 2)) is not int:
        raise ValueError('sources.max_parallel_sources must be an integer')
    for source in enabled:
        provider = provider_settings(settings, source)
        for name in ('max_requests', 'deadline_seconds', 'min_interval_seconds', 'max_attempts', 'connect_timeout_seconds', 'read_timeout_seconds'):
            value = provider[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f'invalid sources.providers.{source}.{name}')
        if type(provider['max_requests']) is not int or type(provider['max_attempts']) is not int or not 1 <= provider['max_attempts'] <= 3:
            raise ValueError('request budget must be an integer and max_attempts must be 1..3')
        for name in ('page_size', 'pages_per_query', 'pages_per_partition', 'papers_per_volume'):
            if name in provider and (type(provider[name]) is not int or provider[name] < 1):
                raise ValueError(f'invalid sources.providers.{source}.{name}')


def provider_settings(settings, source):
    return dict(dict(max_requests=32, deadline_seconds=120, min_interval_seconds=1,
                     max_attempts=2, connect_timeout_seconds=8, read_timeout_seconds=20),
                **{**DEFAULTS.get(source, {}), **settings.get('providers', {}).get(source, {})})


def terminate(process):
    if process.poll() is None:
        if os.name == 'nt':
            # A Windows venv launcher has a separate Python child; stop the whole owned tree.
            stopped = subprocess.run(['taskkill.exe', '/PID', str(process.pid), '/T', '/F'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW, timeout=5)
            if stopped.returncode and process.poll() is None:
                raise RuntimeError('source_process_termination_failed')
        else:
            process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


def consume_pages(store, source, state):
    for path in sorted(state['directory'].glob('cache-*.json')):
        store.save_http_cache(source, json.loads(path.read_text(encoding='utf-8')))
        path.unlink()
    for path in sorted(state['directory'].glob('page-*.json')):
        if path.name in state['pages']:
            continue
        payload = json.loads(path.read_text(encoding='utf-8'))
        counts = {}
        saved = store.save_source_page(source, payload, counts=counts)
        for name, count in counts.items():
            state[name] = state.get(name, 0) + count
        diagnostics = payload.get('diagnostics', {})
        accumulate_diagnostics(state, diagnostics)
        state['records'] += len(saved)
        state['pages'].add(path.name)
        print(f'source={source} page={len(state["pages"])} stored={state["records"]}', flush=True)


def discover(store, config, venues, years, run_id, *, cancel=None, progress=None):
    settings = config['sources']
    validate(settings)
    if settings.get('mode', 'shadow') == 'shadow':
        from state_store import StateStore
        with tempfile.TemporaryDirectory(prefix='scout-shadow-') as tmp:
            shadow = StateStore(Path(tmp)/'state.sqlite3')
            try:
                store.connection.backup(shadow.connection)
                isolated = copy.deepcopy(config)
                isolated['sources']['mode'] = 'production'
                result = discover(shadow, isolated, venues, years, run_id, cancel=cancel, progress=progress)
                result['shadow'] = True
                return result
            finally:
                shadow.close()
    queue = list(settings['enabled'])
    results, active = {}, {}
    started = time.monotonic()
    deadline = started + settings.get('collection_deadline_seconds', 900)
    queries = config.get('discovery_queries') or config.get('explicit_interests', [])[:4]
    queries = [q for q in queries if isinstance(q, str) and q.strip()]
    if not queries:
        raise ValueError('multi-source discovery requires research queries')
    tokens = set(re.findall(r'\w+', ' '.join(queries).lower()))
    delivered = store.delivered_ids()
    missing = [p for p in store.search_candidates(limit=100000) if not official_abstracts.usable_abstract(p.get('abstract', ''))
               and p.get('year') in years and p['id'] not in delivered]
    missing.sort(key=lambda p: -len(tokens & set(re.findall(r'\w+', p.get('title', '').lower()))))
    lookup_budget = max(0, int(config.get('metadata', {}).get('max_requests_per_run', 200)))
    with tempfile.TemporaryDirectory(prefix='scout-sources-') as temporary:
        root = Path(temporary)
        try:
            while queue or active:
                cancelled = cancel is not None and cancel.is_set()
                stopped = time.monotonic() >= deadline or cancelled
                while queue and len(active) < min(4, int(settings.get('max_parallel_sources', 2))) and not stopped and not (cancel and cancel.is_set()):
                    source = queue.pop(0)
                    health = store.source_health(source)
                    current = dt.datetime.now(dt.timezone.utc)
                    if health.get('retry_at', '') > current.isoformat():
                        results[source] = dict(health, status='cooldown', stored=0, requests=0,
                                              new_records=0, abstracts_completed=0, discovered=0, failures={},
                                              deferred=0, deferred_reasons={}, cache_hits=0, bytes=0, seconds=0)
                        continue
                    directory = root / source; directory.mkdir()
                    states = store.source_states(source)
                    if source == 'dblp':
                        for venue in venues:
                            for year in years:
                                task_key = key(source, [venue['dblp_key'], year])
                                old = store.scan_state(venue['dblp_key'], year)
                                if task_key not in states and old:
                                    states[task_key] = dict(offset=old.get('next_offset', 0), exhausted=old.get('status') == 'complete')
                    provider = provider_settings(settings, source)
                    lookups = []
                    for paper in missing:
                        if len(lookups) >= min(8, provider['max_requests']//4, lookup_budget):
                            break
                        target = official_abstracts.official_url(paper) if source in OFFICIAL else None
                        if (target and urlsplit(target).hostname in HOSTS[source]) or (
                            source in ('openalex', 'crossref') and (identity.doi(paper.get('doi')) or identity.doi(paper.get('ee')))):
                            previous = states.get(key(source, ['lookup', paper['id']]), {})
                            if previous.get('retry_at', '') <= current.isoformat():
                                lookups.append(paper)
                    lookup_budget -= len(lookups)
                    task = dict(source=source, settings=provider, states=states, queries=queries,
                                venue_keys=[v['dblp_key'] for v in venues], venues=venues, years=years, lookups=lookups,
                                http_cache=store.http_cache(source))
                    path = directory / 'task.json'
                    path.write_text(json.dumps(task, ensure_ascii=False), encoding='utf-8')
                    log = (directory / 'worker.log').open('wb')
                    try:
                        process = subprocess.Popen([sys.executable, '-m', 'ccf_paper_scout.source_worker', '--task', str(path), '--output', str(directory)],
                            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                    except OSError:
                        result = dict(status='unavailable', code='worker_start_failed', stored=0)
                        store.save_source_health(source, run_id, result)
                        results[source] = result
                        continue
                    finally:
                        log.close()
                    active[source] = dict(process=process, directory=directory, pages=set(), records=0,
                                          deadline=min(deadline, time.monotonic() + provider['deadline_seconds']))
                    print(f'source={source} started budget={provider["max_requests"]} deadline={provider["deadline_seconds"]}s', flush=True)
                cancelled = cancel is not None and cancel.is_set()
                stopped = time.monotonic() >= deadline or cancelled
                for source, state in list(active.items()):
                    consume_pages(store, source, state)
                    timed_out = stopped or time.monotonic() >= state['deadline']
                    if not timed_out and state['process'].poll() is None:
                        continue
                    terminate(state['process'])
                    consume_pages(store, source, state)
                    result_file = state['directory'] / 'result.json'
                    progress_file = state['directory'] / 'progress.json'
                    counters = json.loads(progress_file.read_text(encoding='utf-8')) if progress_file.exists() else {}
                    result = json.loads(result_file.read_text(encoding='utf-8')) if result_file.exists() else dict(
                        status='cancelled' if cancelled else 'partial' if timed_out else 'protocol_error',
                        code='user_cancelled' if cancelled else 'source_deadline' if timed_out else 'worker_failed', **counters)
                    if state['process'].returncode and not timed_out:
                        result.update(status='protocol_error', code='worker_failed')
                    result['stored'] = state['records']
                    result.update({name: state.get(name, 0) for name in ('new_records', 'abstracts_completed')})
                    for name in ('discovered', 'failures', 'deferred', 'deferred_reasons', 'next_entry_retry_at'):
                        if name in state:
                            result[name] = state[name]
                    if result['status'] not in ('ok', 'empty', 'partial', 'not_configured', 'cancelled'):
                        seconds = max(1800, float(result.get('retry_after', 0)))
                        if result['status'] in ('blocked', 'auth_required', 'protocol_error'):
                            seconds = max(seconds, 86400)
                        result['retry_at'] = (dt.datetime.now(dt.timezone.utc)+dt.timedelta(seconds=seconds)).isoformat()
                    store.save_source_health(source, run_id, result)
                    results[source] = result
                    print(f'source={source} status={result["status"]} stored={result["stored"]}', flush=True)
                    del active[source]
                    if progress:
                        progress(dict(providers=dict(results), pending=queue + list(active)))
                if stopped:
                    results.update({s: dict(status='cancelled' if cancelled else 'partial',
                        code='user_cancelled' if cancelled else 'collection_deadline', stored=0) for s in queue})
                    queue.clear()
                if active:
                    time.sleep(.05)
        finally:
            for state in active.values():
                terminate(state['process'])
    healthy = sum(r.get('status') in ('ok', 'empty') for r in results.values())
    stored = sum(r.get('stored', 0) for r in results.values())
    requests = sum(r.get('requests', 0) for r in results.values())
    mode = 'cached_only' if not requests else 'live' if healthy == len(results) else 'partial_live' if stored else 'cached_only'
    return dict(mode=mode, providers=results, stored=stored, seconds=round(time.monotonic()-started, 3))
