"""Offline evaluation of frozen search results with explicit label provenance."""
from __future__ import annotations

import copy
import math

from . import search_history as history


def export(store, identifiers=None):
    if identifiers is None:
        identifiers = [r[0] for r in store.connection.execute(
            "SELECT search_id FROM search_sessions WHERE state IN ('complete','cancelled') ORDER BY started_at DESC")]
    queries, seen = [], set()
    for identifier in identifiers:
        result = history.get(store, identifier)
        if not result or result['state'] not in ('complete', 'cancelled'):
            raise ValueError('export completed or cancelled searches only')
        if result['query_key'] in seen:
            continue  # One snapshot per normalized query; no repeated-query weighting.
        seen.add(result['query_key'])
        papers = [{k: v for k, v in p.items() if k not in ('feedback', 'judgments')} for p in result['results']]
        queries.append(dict(search_id=identifier, query=result['query'], query_key=result['query_key'],
            request=result['request'], split=result['split'], snapshot_hash=result['snapshot_hash'], papers=papers,
            ranking=[p['id'] for p in papers], judgments={p['id']: p['judgments'] for p in result['results']},
            timing=result.get('timing', {}), discovery=result['discovery'], counts=result.get('counts', {})))
    return dict(schema='scout-search-evaluation-v1', queries=queries)


def validate(dataset):
    if dataset.get('schema') != 'scout-search-evaluation-v1' or not isinstance(dataset.get('queries'), list):
        raise ValueError('unsupported evaluation snapshot')
    seen = set()
    for query in dataset['queries']:
        if query['query_key'] in seen or query['split'] not in ('dev', 'test'):
            raise ValueError('duplicate query or invalid split')
        seen.add(query['query_key'])
        if (query['query'] != query['request']['query'] or query['split'] != query['request']['split']
                or query['query_key'] != history.query_key(query['query'])):
            raise ValueError('query context does not match frozen request')
        if history.digest(dict(request=query['request'], results=query['papers'])) != query['snapshot_hash']:
            raise ValueError('snapshot fingerprint mismatch; do not edit frozen papers')
        identifiers = [p['id'] for p in query['papers']]
        if len(identifiers) != len(set(identifiers)) or query['ranking'] != identifiers:
            raise ValueError('invalid original ranking')
        for paper_id, labels in query['judgments'].items():
            if paper_id not in identifiers or set(labels) - {'human', 'ai'}:
                raise ValueError('invalid judgment identity or origin')
            for origin, label in labels.items():
                grade = label.get('grade')
                if grade is not None and (type(grade) is not int or grade not in (0, 1, 2)):
                    raise ValueError('invalid grade')
                if label.get('origin') != origin or label.get('snapshot_hash') != query['snapshot_hash']:
                    raise ValueError('invalid judgment provenance')
                if query['split'] == 'test' and origin == 'ai':
                    raise ValueError('AI judgments are not permitted in held-out test queries')


def ai_packet(dataset):
    validate(dataset)
    examples, tasks, deferred = [], [], 0
    for query in dataset['queries']:
        if query['split'] != 'dev':
            continue
        for paper in query['papers']:
            labels = query['judgments'].get(paper['id'], {})
            item = dict(search_id=query['search_id'], snapshot_hash=query['snapshot_hash'], query=query['query'],
                        paper_id=paper['id'], title=paper['title'], abstract=paper.get('abstract', ''))
            if labels.get('human', {}).get('grade') is not None:
                examples.append(dict(item, **{k: labels['human'][k] for k in ('grade', 'reason')}))
            elif 'human' not in labels and 'ai' not in labels:
                if item['abstract']:
                    tasks.append(dict(item, grade=None, reason='', actor=''))
                else:
                    deferred += 1
    tasks.sort(key=lambda item: history.digest([item['search_id'], item['paper_id']]))
    return dict(schema='scout-ai-labels-v1', instructions=(
        'Use human development examples as guidance. Grade relevance to the specific query: '
        '2=directly useful, 1=partly useful, 0=irrelevant. Leave null when evidence is insufficient. '
        'Explain with title/abstract evidence and identify the annotator/model. Do not infer from ranking. '
        'No held-out queries or labels are included. Import always records these as AI suggestions.'),
        examples=examples, labels=tasks, deferred_missing_abstract=deferred)


def import_ai(store, packet):
    if packet.get('schema') != 'scout-ai-labels-v1' or not isinstance(packet.get('labels'), list):
        raise ValueError('unsupported AI annotation packet')
    count = 0
    with store.connection:
        for item in packet['labels']:
            # A downloaded but untouched template is not an annotation.
            if not item.get('reason') and not item.get('actor') and item.get('grade') is None:
                continue
            history.label(store, item['search_id'], item['paper_id'], item['grade'], origin='ai',
                reason=item['reason'], actor=item['actor'], snapshot_hash=item['snapshot_hash'])
            count += 1
    return count


def evaluate(dataset, *, k=5, ranking='stored', overrides=None):
    validate(dataset)
    if type(k) is not int or not 1 <= k <= 100 or ranking not in ('stored', 'lexical'):
        raise ValueError('invalid k or ranking')
    if overrides is not None and (overrides.get('schema') != 'scout-search-rankings-v1' or
                                 not isinstance(overrides.get('queries'), dict)):
        raise ValueError('invalid comparison rankings')
    if overrides is not None and set(overrides['queries']) != {q['search_id'] for q in dataset['queries']}:
        raise ValueError('comparison must contain exactly the same queries')
    rows = []
    for query in dataset['queries']:
        order = query['ranking']
        if overrides is not None:
            candidate = overrides['queries'][query['search_id']]
            order = candidate['ranking']
            if candidate['snapshot_hash'] != query['snapshot_hash'] or len(order) != len(set(order)) or set(order) != set(query['ranking']):
                raise ValueError('comparison must rank the same frozen paper pool exactly once')
        elif ranking == 'lexical':
            import paper_scout
            order = [p['id'] for p in paper_scout.rank_candidates([], copy.deepcopy(query['papers']), [query['query']])]
        human = {i: values['human']['grade'] for i, values in query['judgments'].items()
                 if 'human' in values and values['human']['grade'] is not None}
        top = order[:k]
        fully_judged_top = bool(top) and all(i in human for i in top)
        precision = sum(human[i] >= 1 for i in top)/k if fully_judged_top else None
        strong_precision = sum(human[i] == 2 for i in top)/k if fully_judged_top else None
        ndcg = None
        if order and all(i in human for i in order):
            def dcg(grades):
                return sum((2**g-1)/math.log2(n+2) for n, g in enumerate(grades))
            ideal = dcg(sorted(human.values(), reverse=True)[:k])
            ndcg = dcg([human[i] for i in top])/ideal if ideal else None
        rows.append(dict(search_id=query['search_id'], query=query['query'], split=query['split'],
            pool_size=len(order), judged_human=len(human), top_judged=sum(i in human for i in top), top_size=len(top),
            ai_suggestions=sum('ai' in labels for labels in query['judgments'].values()),
            precision_at_k=precision, strong_precision_at_k=strong_precision, ndcg_at_k=ndcg,
            cache_seconds=query.get('timing', {}).get('cache_seconds'),
            total_seconds=query.get('timing', {}).get('total_seconds')))
    summaries = {}
    for split in ('dev', 'test'):
        group = [r for r in rows if r['split'] == split]
        summary = dict(queries=len(group))
        for metric in ('precision_at_k', 'strong_precision_at_k', 'ndcg_at_k'):
            values = [r[metric] for r in group if r[metric] is not None]
            summary[metric] = sum(values)/len(values) if values else None
            summary[metric + '_queries'] = len(values)
        summaries[split] = summary
    return dict(k=k, ranking='external' if overrides is not None else ranking, labels='human_only',
        interpretation='P@k requires every returned top-k item judged; missing result slots count as zero. '
        'nDCG requires every item in the captured pool judged, and a positive ideal gain. '
        'Unjudged items are not assigned zero. AI suggestions never enter these metrics. '
        'This measures the captured result pool, not corpus recall; dev and test are reported separately.',
        summary=summaries, queries=rows)
