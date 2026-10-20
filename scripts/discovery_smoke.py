"""Small public-network integration check; never accesses Zotero, SMTP or private state."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import paper_scout
from ccf_paper_scout.discovery import discover
from state_store import StateStore


def main() -> None:
    venue = {"dblp_key": "aaai", "abbr": "AAAI", "name": "AAAI", "rank": "A", "type": "conference"}
    config = {"transport": "sparql", "page_size": 2, "max_pages_per_run": 1,
              "max_pages_per_partition_per_run": 1, "request_delay_seconds": 3, "max_attempts": 2}
    with tempfile.TemporaryDirectory() as directory:
        store = StateStore(Path(directory) / "discovery.sqlite3")
        try:
            first, _ = discover(store, {"aaai": venue}, [2026], config, "ccf-paper-scout/0.2", paper_scout.fetch_dblp_page)
            offset = store.load_cursor("DBLP", "aaai", 2026)
            second, _ = discover(store, {"aaai": venue}, [2026], config, "ccf-paper-scout/0.2", paper_scout.fetch_dblp_page)
            assert offset == 2 and store.load_cursor("DBLP", "aaai", 2026) == 4
            assert len(store.candidates(["aaai"], [2026])) == 4
            print(json.dumps({"first": first, "second": second, "saved_candidates": 4}, indent=2))
        finally:
            store.close()


if __name__ == "__main__":
    main()
