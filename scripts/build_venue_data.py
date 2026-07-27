#!/usr/bin/env python3
"""Rebuild the derived CCF-A venue mapping from a pinned CCFrank4dblp commit."""
from __future__ import annotations

import argparse
import json
import re
import urllib.request
from pathlib import Path

COMMIT = "540396b36bfb46b18cfed22bf5c578d73257c4b9"
BASE = f"https://raw.githubusercontent.com/WenyanLiu/CCFrank4dblp/{COMMIT}/data"
FILES = {
    "rank": "ccfRankUrl.js",
    "name": "ccfRankFull.js",
    "abbr": "ccfRankAbbr.js",
}
ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "ccf_a_venues.json"


def parse_mapping(text: str) -> dict[str, str]:
    return dict(re.findall(r'^\s*"([^"]+)":\s*"([^"]*)",?\s*$', text, re.MULTILINE))


def fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "ccf-paper-scout-venue-builder/0.1"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8")


def build() -> dict:
    mappings = {key: parse_mapping(fetch_text(f"{BASE}/{filename}")) for key, filename in FILES.items()}
    venues = []
    aliases_removed: dict[str, list[str]] = {}
    seen: set[tuple[str, str]] = set()
    for path, rank in mappings["rank"].items():
        if rank != "A":
            continue
        kind = "conference" if path.startswith("/conf/") else "journal"
        parts = path.strip("/").split("/")
        dblp_key = parts[1]
        runtime_key = (kind, dblp_key.lower())
        if runtime_key in seen:
            aliases_removed.setdefault(f"{kind}:{dblp_key.lower()}", []).append(path)
            continue
        seen.add(runtime_key)
        venues.append({
            "id": path,
            "type": kind,
            "dblp_key": dblp_key,
            "abbr": mappings["abbr"].get(path, ""),
            "name": mappings["name"].get(path, ""),
            "rank": "A",
        })
    return {
        "source": "https://github.com/WenyanLiu/CCFrank4dblp",
        "source_commit": COMMIT,
        "source_files": [f"data/{name}" for name in FILES.values()],
        "license": "MIT",
        "note": "Derived CCF venue metadata for whitelist constraints; not an official CCF API. Verify against the latest official catalog.",
        "aliases_removed": aliases_removed,
        "venues": venues,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        current = json.loads(OUTPUT.read_text(encoding="utf-8"))
        if current.get("source_commit") != COMMIT:
            raise SystemExit("venue data source_commit does not match the pinned builder commit")
        keys = [(venue["type"], venue["dblp_key"].lower()) for venue in current.get("venues", [])]
        if len(keys) != len(set(keys)) or not keys:
            raise SystemExit("venue data contains duplicate runtime keys or is empty")
        if any(venue.get("rank") != "A" for venue in current["venues"]):
            raise SystemExit("venue data contains a non-A record")
        print(f"venue data check passed: {len(keys)} records at {COMMIT}")
        return 0
    generated = json.dumps(build(), ensure_ascii=False, indent=2) + "\n"
    OUTPUT.write_text(generated, encoding="utf-8")
    print(f"wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
