from __future__ import annotations

import json
from importlib.resources import files

from ..models import Paper


def _normalize(value: str) -> str:
    return " ".join(value.lower().replace("&", "and").replace("-", " ").split())


def load_control_venues() -> dict[str, dict]:
    path = files("ccf_paper_scout").joinpath("data/control_venues.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    result: dict[str, dict] = {}
    for venue in payload.get("venues", []):
        names = [venue["canonical_name"], *venue.get("aliases", [])]
        for name in names:
            key = _normalize(name)
            if key in result:
                raise RuntimeError(f"duplicate control venue alias: {name}")
            result[key] = venue
    return result


def apply_control_policy(papers: list[Paper], venues: dict[str, dict] | None = None) -> list[Paper]:
    venues = venues or load_control_venues()
    for paper in papers:
        venue = venues.get(_normalize(paper.venue_name))
        if not venue:
            continue
        paper.venue_id = str(venue["venue_id"])
        paper.channel = "formal_control"
    return papers
