"""Small public-source end-to-end probe. Never uses Zotero, SMTP, or an LLM."""
from __future__ import annotations

import argparse
import contextlib
from importlib.resources import files
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paper_scout
from ccf_paper_scout import source_adapters


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--sources', nargs='+', choices=list(source_adapters.HOSTS), default=['pmlr', 'openalex'])
    parser.add_argument('--max-requests', type=int, default=3)
    parser.add_argument('--deadline', type=int, default=45)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='scout-public-probe-') as temp:
        root = Path(temp)
        config = dict(years=[2025], venue_keys=['icml', 'nips', 'acl', 'aaai', 'cvpr'], max_results=5,
            state_db=str(root/'state.sqlite3'), seen_db=str(root/'seen.json'),
            explicit_interests=['world model', 'reinforcement learning'],
            topic_priority={'primary_topics': ['world model', 'reinforcement learning']},
            discovery_queries=['world model reinforcement learning'],
            sources=dict(enabled=args.sources, mode='production', max_parallel_sources=2,
                collection_deadline_seconds=args.deadline*len(args.sources),
                providers={s: dict(max_requests=args.max_requests, deadline_seconds=args.deadline, page_size=3) for s in args.sources}),
            metadata={'max_requests_per_run': 0}, delivery={'smtp': {'enabled': False}},
            semantic={'enabled': False}, llm_translation={'enabled': False})
        (root/'config.json').write_text(json.dumps(config), encoding='utf-8')
        (root/'interests.json').write_text('[]', encoding='utf-8')
        run_args = argparse.Namespace(config=root/'config.json', interests=root/'interests.json',
            venues=Path(str(files('ccf_paper_scout').joinpath('data/ccf_a_venues.json'))),
            output=args.output/'recommendations.md', labels=None, no_update_seen=True, discover_only=False, evaluate_only=False)
        with (args.output/'run.log').open('w', encoding='utf-8') as log, contextlib.redirect_stdout(log):
            paper_scout.run_pipeline(run_args, config)
        data = json.loads((args.output/'recommendations.diagnostics.json').read_text(encoding='utf-8'))
        summary = dict(discovery=data['discovery'], selected_count=len(data['selected_ids']), qualification=data['qualification'],
                       emails_sent=0, private_library_used=False)
        (args.output/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    main()
