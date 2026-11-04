"""Manual retrieval reuses discovery and ranking, with a separate local library."""
from __future__ import annotations

import contextlib
import copy
from importlib.resources import files
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import uuid

from state_store import StateStore
from . import discovery, official_abstracts, proceedings, sources, search_history
from .source_adapters import HOSTS

SCOPES = ('verified_main', 'published', 'all_with_preprints')


def database(config, config_path):
    base = config_path.resolve().parent
    path = Path(config.get('search', {}).get('state_db', 'state/search.sqlite3'))
    path = path if path.is_absolute() else base / path
    daily = Path(config.get('state_db', 'state/paper_scout.sqlite3'))
    daily = daily if daily.is_absolute() else base / daily
    if path.resolve() == daily.resolve():
        raise ValueError('search.state_db must differ from the Daily database')
    path.parent.mkdir(parents=True, exist_ok=True)
    # Seed once through SQLite's snapshot API, including WAL; never copy a live file.
    if not path.exists() and daily.exists():
        with contextlib.closing(sqlite3.connect(daily.resolve().as_uri() + '?mode=ro', uri=True)) as src:
            with contextlib.closing(sqlite3.connect(path)) as dst:
                src.backup(dst)
    return path


def settings(config, query, enabled=None, years=None):
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 500:
        raise ValueError('query must contain 1..500 characters')
    import paper_scout
    if not paper_scout.tokenize(query):
        raise ValueError('Please include searchable keywords (English recommended)')
    enabled = enabled if enabled is not None else ['openalex', 'arxiv']
    if not isinstance(enabled, list) or not enabled or any(s not in HOSTS for s in enabled):
        raise ValueError('choose supported sources')
    years = years if years is not None else discovery.resolve_years(config)
    if not isinstance(years, list) or not years or len(years) > 10 or any(type(y) is not int or not 1900 <= y <= 2100 for y in years):
        raise ValueError('years must contain 1..10 integer years in 1900..2100')
    result = copy.deepcopy(config)
    result.update(years=years, discovery_queries=[query.strip()])
    result['metadata'] = {'max_requests_per_run': 0}
    result['sources'] = dict(enabled=enabled, mode='production', max_parallel_sources=2,
        collection_deadline_seconds=60, providers={s: {
            **config.get('sources', {}).get('providers', {}).get(s, {}),
            'max_requests': 4, 'deadline_seconds': 30, 'page_size': 25,
            'pages_per_query': 1, 'papers_per_volume': 2} for s in enabled})
    sources.validate(result['sources'])
    return result


def cached_results(store, query, years, scope, limit, *, counts=None):
    import paper_scout
    if scope not in SCOPES or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('invalid publication_scope or limit (1..100)')
    registry = proceedings.registry()
    candidates = []
    stats = dict(cached=0, year_or_identity_filtered=0, publication_filtered=0, zero_match=0)
    for paper in store.search_candidates(limit=100000):
        stats['cached'] += 1
        if paper.get('year') not in years or paper.get('identity_review_pending'):
            stats['year_or_identity_filtered'] += 1
            continue
        paper['abstract'] = official_abstracts.usable_abstract(paper.get('abstract', ''))
        verified = proceedings.qualify(paper, registry) == 'verified'
        publication = 'verified_main' if verified else paper.get('publication_status', 'unknown')
        if scope == 'verified_main' and not verified:
            stats['publication_filtered'] += 1
            continue
        if scope == 'published' and publication not in ('published', 'verified_main'):
            stats['publication_filtered'] += 1
            continue
        paper['publication_label'] = publication
        candidates.append(paper)
    ranked = paper_scout.rank_candidates([], candidates, [query])
    stats.update(zero_match=sum(p['lexical_score'] <= 0 for p in ranked), matched=sum(p['lexical_score'] > 0 for p in ranked))
    stats['displayed'] = min(limit, stats['matched'])
    if counts is not None:
        counts.update(stats)
    feedback = {r[0]: json.loads(r[1]) for r in store.connection.execute('SELECT paper_id,payload_json FROM paper_feedback')}
    return [{**{k: p.get(k) for k in ('id', 'title', 'authors', 'year', 'abstract', 'url', 'ee', 'doi',
                'venue', 'publication_label', 'found_in', 'abstract_source', 'lexical_score')},
             'feedback': feedback.get(p['id'], {})} for p in ranked if p['lexical_score'] > 0][:limit]


def run(config, path, query, *, enabled=None, years=None, scope='all_with_preprints', limit=20,
        cached_only=False, cancel=None, update=None, search_id=None, split='dev'):
    configured = settings(config, query, enabled, years)
    if scope not in SCOPES or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('invalid publication scope or limit')
    request = dict(query=query.strip(), sources=configured['sources']['enabled'], years=configured['years'],
                   publication_scope=scope, limit=limit, cached_only=cached_only, split=split)
    store = StateStore(path)
    identifier = search_id or uuid.uuid4().hex
    result = None
    started = time.monotonic()
    try:
        search_history.start(store, identifier, request)
        result = search_history.get(store, identifier)
        counts = {}
        result.update(state='cached', ranking='lexical', ranking_version='lexical-v1',
            results=cached_results(store, query, configured['years'], scope, limit, counts=counts), counts=counts,
            discovery={'mode': 'cached_only', 'providers': {}})
        result['timing'] = {'cache_seconds': round(time.monotonic()-started, 3)}
        search_history.save(store, result)
        if update:
            update(result)
        if not cached_only and not (cancel and cancel.is_set()):
            venues = json.loads(files('ccf_paper_scout').joinpath('data/ccf_a_venues.json').read_text(encoding='utf-8'))
            venues = [v for v in venues['venues'] if v['dblp_key'] in config.get('venue_keys', [])]
            def progress(status):
                result.update(state='searching', discovery=status,
                    results=cached_results(store, query, configured['years'], scope, limit, counts=counts))
                search_history.save(store, result)
                if update:
                    update(result)
            result['discovery'] = sources.discover(store, configured, venues, configured['years'],
                identifier, cancel=cancel, progress=progress)
            result['results'] = cached_results(store, query, configured['years'], scope, limit, counts=counts)
        result['state'] = 'cancelled' if cancel and cancel.is_set() else 'complete'
        result['timing']['total_seconds'] = round(time.monotonic()-started, 3)
        search_history.save(store, result)
        return search_history.get(store, identifier)
    except Exception as exc:
        if result is not None and result.get('state') not in search_history.TERMINAL:
            result.update(state='failed', error=type(exc).__name__)
            search_history.save(store, result)
        raise
    finally:
        store.close()


def doctor(config, network=False):
    import os
    enabled = config.get('sources', {}).get('enabled', [])
    sources.validate(config.get('sources', {}))
    credentials = {'openalex': ('OPENALEX_API_KEY',),
        'semantic_scholar': ('SEMANTIC_SCHOLAR_API_KEY', 'SEMANTICSCHOLAR_API_KEY'),
        'crossref': ('CROSSREF_MAILTO',)}
    for source in enabled:
        names = credentials.get(source, ())
        print(f'{source}: configured' + (f'; credential={"present" if any(os.environ.get(n) for n in names) else "absent"}' if names else ''))
    if not network or not enabled:
        return 0
    probe = copy.deepcopy(config)
    probe['metadata'] = {'max_requests_per_run': 0}
    probe['discovery_queries'] = ['world model']
    probe['sources'].update(collection_deadline_seconds=60, max_parallel_sources=2,
        providers={s: {**probe['sources'].get('providers', {}).get(s, {}),
                      'max_requests': 2, 'max_attempts': 1, 'deadline_seconds': 20, 'page_size': 1,
                      'papers_per_volume': 1, 'pages_per_query': 1} for s in enabled})
    with tempfile.TemporaryDirectory(prefix='scout-doctor-') as tmp:
        store = StateStore(Path(tmp)/'state.sqlite3')
        try:
            venues = json.loads(files('ccf_paper_scout').joinpath('data/ccf_a_venues.json').read_text(encoding='utf-8'))
            result = sources.discover(store, probe, [v for v in venues['venues'] if v['dblp_key'] in config['venue_keys']],
                discovery.resolve_years(config), 'doctor')
        finally:
            store.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if all(r['status'] in ('ok', 'empty', 'partial') for r in result['providers'].values()) else 2
