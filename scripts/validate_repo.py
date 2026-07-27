#!/usr/bin/env python3
"""Fail-fast checks for files shipped in the public repository."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"repository validation failed: {message}")


def main() -> None:
    for relative in ("config.example.json", "config.test.json", "data/ccf_a_venues.json"):
        json.loads((ROOT / relative).read_text(encoding="utf-8"))

    data = json.loads((ROOT / "data/ccf_a_venues.json").read_text(encoding="utf-8"))
    require(bool(data.get("source_commit")), "venue data lacks pinned source_commit")
    ids = [venue["id"] for venue in data["venues"]]
    runtime_keys = [(venue["type"], venue["dblp_key"].lower()) for venue in data["venues"]]
    require(len(ids) == len(set(ids)), "duplicate venue id")
    require(len(runtime_keys) == len(set(runtime_keys)), "duplicate runtime venue key")
    require(all(venue.get("rank") == "A" for venue in data["venues"]), "non-A venue in A-only mapping")

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    require("/home/zlw" not in readme, "README contains a developer-specific absolute path")
    require((ROOT / "LICENSE").exists(), "LICENSE is missing")
    require((ROOT / "THIRD_PARTY_NOTICES.md").exists(), "third-party notices are missing")

    tracked = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT, check=True,
        text=True, capture_output=True,
    ).stdout.splitlines()
    public_text = "\n".join(
        (ROOT / relative).read_text(encoding="utf-8", errors="ignore")
        for relative in tracked
        if (ROOT / relative).is_file() and Path(relative).name != "validate_repo.py"
    )
    secret_patterns = (r"sk-[A-Za-z0-9_-]{16,}", r"gh[pousr]_[A-Za-z0-9]{20,}")
    require(not any(re.search(pattern, public_text) for pattern in secret_patterns), "possible hard-coded secret")
    print(f"repository validation passed: {len(data['venues'])} unique CCF-A venue mappings")


if __name__ == "__main__":
    main()
