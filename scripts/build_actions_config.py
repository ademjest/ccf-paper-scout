#!/usr/bin/env python3
"""Build a private ephemeral GitHub Actions config without embedding credentials."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def build(base: Path, output: Path, state_dir: Path, max_results: int, smtp_enabled: bool = True) -> None:
    required = ("SMTP_SENDER", "SMTP_RECEIVER", "LLM_BASE_URL", "LLM_MODEL")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError("missing Actions configuration: " + ", ".join(missing))
    payload = json.loads(base.read_text(encoding="utf-8"))
    payload["max_results"] = max_results
    payload["seen_db"] = str(state_dir / "seen.json")
    payload["state_db"] = str(state_dir / "paper_scout.sqlite3")
    payload["run_lock"] = str(state_dir / "paper_scout.lock")
    llm = payload.setdefault("llm_translation", {})
    llm.update({
        "enabled": True,
        "base_url": os.environ["LLM_BASE_URL"],
        "model": os.environ["LLM_MODEL"],
        "api_key_env": "LLM_API_KEY",
        "language": "简体中文",
        "cache": str(state_dir / "translations.json"),
    })
    smtp = payload.setdefault("delivery", {}).setdefault("smtp", {})
    smtp.update({
        "enabled": smtp_enabled, "host": "smtp.qq.com", "port": 465, "use_ssl": True,
        "sender": os.environ["SMTP_SENDER"], "receiver": os.environ["SMTP_RECEIVER"],
        "password_env": "SMTP_PASSWORD", "timeout_seconds": 60,
    })
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    output.chmod(0o600)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--max-results", type=int, default=5)
    parser.add_argument("--disable-smtp", action="store_true")
    args = parser.parse_args()
    build(args.base, args.output, args.state_dir, args.max_results, not args.disable_smtp)
    print(f"Actions config generated at {args.output} (secrets remain environment-only)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
