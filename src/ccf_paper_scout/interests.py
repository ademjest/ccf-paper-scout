"""Resolve explicit research profiles independently of Zotero credentials."""
from __future__ import annotations

import json
from pathlib import Path


def configure(config: dict, base: Path) -> None:
    profile_file = config.pop('research_profile_file', None)
    if profile_file:
        path = Path(profile_file)
        profile = json.loads((path if path.is_absolute() else base / path).read_text(encoding='utf-8-sig'))
        if not isinstance(profile, dict):
            raise ValueError('research profile must be a JSON object')
        keywords = profile.get('explicit_interests')
        if not isinstance(keywords, list) or not keywords or any(not isinstance(s, str) or not s.strip() for s in keywords):
            raise ValueError('research profile requires nonempty explicit_interests')
        # Omitted fields derive from this profile, never inherit another user's topics.
        config['explicit_interests'] = keywords
        config['discovery_queries'] = profile.get('discovery_queries', keywords)
        config['research_profile'] = profile.get('research_profile', {
            'directions': [{'name': s, 'description': s} for s in keywords]})
        config['topic_priority'] = profile.get('topic_priority', {
            'primary_topics': keywords, 'exploration_topics': [], 'max_exploration_results': 0})
        if not isinstance(config['topic_priority'], dict) or not isinstance(config['research_profile'], dict):
            raise ValueError('topic_priority and research_profile must be JSON objects')
        if 'venue_keys' in profile:
            config['venue_keys'] = profile['venue_keys']
        for name, values in [('discovery_queries', config['discovery_queries']),
                             ('primary_topics', config['topic_priority'].get('primary_topics'))]:
            if not isinstance(values, list) or not values or any(not isinstance(s, str) or not s.strip() for s in values):
                raise ValueError(f'research profile requires nonempty {name}')
        directions = config['research_profile'].get('directions')
        if not isinstance(directions, list) or not directions or any(
                not isinstance(d, dict) or any(not isinstance(d.get(k), str) or not d[k].strip()
                for k in ('name', 'description')) for d in directions):
            raise ValueError('research directions require a name and description')
    source = config.get('interest_source', 'zotero')
    if source not in ('zotero', 'manual'):
        raise ValueError('interest_source must be zotero or manual')
    if source == 'manual':
        keywords = config.get('explicit_interests')
        if not isinstance(keywords, list) or not keywords or any(not isinstance(s, str) or not s.strip() for s in keywords):
            raise ValueError('manual interests require nonempty explicit_interests')
        from paper_scout import tokenize
        if not tokenize(' '.join(keywords)):
            raise ValueError('manual interests require searchable keywords (English recommended)')
