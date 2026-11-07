#!/usr/bin/env python3
"""Build a private ephemeral GitHub Actions config without embedding credentials."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def environment_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name, '').strip().lower()
    if not value:
        return default
    if value not in ('true', 'false'):
        raise ValueError(f'{name} must be true or false')
    return value == 'true'


def build(base: Path, output: Path, state_dir: Path, max_results: int, smtp_enabled: bool = True) -> None:
    if not 1 <= max_results <= 20:
        raise ValueError("max_results must be 1..20")
    llm_enabled = environment_bool('LLM_ENABLED', bool(os.environ.get('LLM_BASE_URL') and os.environ.get('LLM_MODEL')))
    required = (['SMTP_SENDER', 'SMTP_RECEIVER'] if smtp_enabled else []) + (
        ['LLM_BASE_URL', 'LLM_MODEL'] if llm_enabled else [])
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError("missing Actions configuration: " + ", ".join(missing))
    payload = json.loads(base.read_text(encoding="utf-8"))
    profile_name = os.environ.get("RESEARCH_PROFILE", "legacy") or "legacy"
    if profile_name not in ("legacy", "rl-action-world-models", "custom"):
        raise ValueError("RESEARCH_PROFILE must be legacy, rl-action-world-models or custom")
    if profile_name != "legacy":
        payload['research_profile_file'] = str(Path(__file__).resolve().parents[1] / 'profiles' / (profile_name + '.json'))
    payload['interest_source'] = os.environ.get('INTEREST_SOURCE') or payload.get('interest_source', 'zotero')
    from ccf_paper_scout.interests import configure
    configure(payload, base.resolve().parent)
    payload["research_profile_name"] = profile_name
    semantic_mode = os.environ.get("SEMANTIC_MODE", "off") or "off"
    if semantic_mode not in ("off", "shadow", "production"):
        raise ValueError("SEMANTIC_MODE must be off, shadow or production")
    payload.setdefault("semantic", {})["enabled"] = semantic_mode != "off"
    payload["semantic"]["mode"] = "production" if semantic_mode == "production" else "shadow"
    relation_mode = os.environ.get("RELATION_MODE", "shadow") or "shadow"
    if relation_mode not in ("off", "shadow", "production"):
        raise ValueError("RELATION_MODE must be off, shadow or production")
    payload.setdefault("relation_policy", {}).update(enabled=relation_mode != 'off',
                                                    mode='production' if relation_mode == 'production' else 'shadow',
                                                    calibration_file=str(state_dir / "relation-calibration.json"))
    if not llm_enabled:
        payload['relation_policy'].setdefault('review', {})['enabled'] = False
        payload.setdefault('relevance_review', {})['enabled'] = False
    payload.setdefault("selection", {})["calibration_file"] = str(state_dir / "calibration.json")
    payload["max_results"] = max_results
    payload["seen_db"] = str(state_dir / "seen.json")
    payload["state_db"] = str(state_dir / "paper_scout.sqlite3")
    payload["run_lock"] = str(state_dir / "paper_scout.lock")
    llm = payload.setdefault("llm_translation", {})
    llm.update({
        "enabled": llm_enabled,
        "base_url": os.environ.get('LLM_BASE_URL') or llm.get('base_url', ''),
        "model": os.environ.get('LLM_MODEL') or llm.get('model', ''),
        "api_key_env": "LLM_API_KEY",
        "language": "简体中文",
        "cache": str(state_dir / "translations.json"),
    })
    smtp = payload.setdefault("delivery", {}).setdefault("smtp", {})
    use_ssl = environment_bool('SMTP_USE_SSL', True)
    port = int(os.environ.get('SMTP_PORT') or (465 if use_ssl else 587))
    if not 1 <= port <= 65535:
        raise ValueError('SMTP_PORT must be 1..65535')
    smtp.update({
        "enabled": smtp_enabled, "host": os.environ.get('SMTP_HOST') or 'smtp.qq.com',
        "port": port, "use_ssl": use_ssl,
        "sender": os.environ.get('SMTP_SENDER', ''), "receiver": os.environ.get('SMTP_RECEIVER', ''),
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
    parser.add_argument("--max-results", type=int, default=10)
    parser.add_argument("--disable-smtp", action="store_true")
    args = parser.parse_args()
    build(args.base, args.output, args.state_dir, args.max_results, not args.disable_smtp)
    print(f"Actions config generated at {args.output} (secrets remain environment-only)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
