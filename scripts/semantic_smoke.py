"""Public synthetic health check; not a research-quality evaluation."""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

from ccf_paper_scout.semantic import rank
from state_store import StateStore


def main() -> None:
    config = {"semantic": {"enabled": True}, "relation_policy": {"enabled": True},
              "explicit_interests": ["multi-agent reinforcement learning"]}
    papers = [{"id": "smoke:rl", "title": "Cooperative multi-agent reinforcement learning", "abstract": "Policies learn coordination and rewards in a sequential decision problem."},
              {"id": "smoke:other", "title": "Cataloging medieval pottery", "abstract": "Archaeological classification of ceramic fragments and historical trade routes."}]
    with tempfile.TemporaryDirectory() as directory:
        store = StateStore(Path(directory) / "smoke.sqlite3")
        try:
            started = time.monotonic()
            first = rank(papers, [], config, store)
            elapsed = time.monotonic() - started
            second = rank(papers, [], config, store)
            assert first["encoded"] == 2 and second["cache_hits"] == 2 and second["encoded"] == 0
            assert papers[0]["semantic_score"] > papers[1]["semantic_score"]
            assert second["relations"]["prototype_cache_hits"] == 9
            assert len(papers[0]["relation_scores"]) == 3
            print(json.dumps({"first_run_seconds": round(elapsed, 3), "cold": first, "warm": second}, indent=2))
        finally:
            store.close()


if __name__ == "__main__":
    main()
