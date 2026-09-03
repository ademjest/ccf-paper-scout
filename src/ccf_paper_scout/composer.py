from __future__ import annotations

from .models import Paper


def compose_digest(papers: list[Paper], digest: dict) -> list[Paper]:
    limit = max(0, int(digest.get("max_results", 10)))
    quotas = dict(digest.get("quotas", {}))
    selected: list[Paper] = []
    seen: set[str] = set()
    channel_order = list(quotas) or ["formal", "preprint", "exploration", "other"]
    for channel in channel_order:
        quota = max(0, int(quotas.get(channel, limit)))
        candidates = sorted((paper for paper in papers if paper.channel == channel), key=lambda paper: paper.score, reverse=True)
        for paper in candidates[:quota]:
            if paper.canonical_id not in seen and len(selected) < limit:
                selected.append(paper)
                seen.add(paper.canonical_id)
    return selected
