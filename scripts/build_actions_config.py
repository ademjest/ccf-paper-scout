#!/usr/bin/env python3
"""Build a private ephemeral GitHub Actions config without embedding credentials."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

PROFILE_KEYS = {"version", "sources", "domains", "primary", "exploration", "digest", "eligibility"}
SOURCE_KEYS = {"years", "venue_keys", "zotero_collection_keys", "recent_interest_items", "zotero_dedup_items", "openalex_enrich_limit", "dblp", "arxiv", "ieee_xplore"}
DIGEST_KEYS = {"min_score", "primary_topic_boost", "exploration_topic_boost", "max_exploration_results", "quotas"}
CREDENTIAL_KEY_PARTS = {"password", "passwd", "secret", "token", "credential", "authorization", "auth"}


def _reject_credential_keys(value: object, path: str = "profile") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if (
                set(normalized.split("_")) & CREDENTIAL_KEY_PARTS
                or normalized in {"apikey", "api_key"}
                or normalized.endswith("_key")
            ):
                raise ValueError(f"profile contains credential-like key: {path}.{key}")
            _reject_credential_keys(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_credential_keys(child, f"{path}[{index}]")


def _string_list(profile: dict[str, object], name: str) -> list[str]:
    value = profile[name]
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"profile.{name} must be a list of non-empty strings")
    return list(value)


def load_profile(raw: str) -> dict[str, object]:
    try:
        profile = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"PAPER_SCOUT_PROFILE_JSON must be valid JSON: {exc.msg}") from None
    if not isinstance(profile, dict):
        raise ValueError("PAPER_SCOUT_PROFILE_JSON must be a JSON object")
    _reject_credential_keys(profile)
    unknown = set(profile) - PROFILE_KEYS
    required_keys = PROFILE_KEYS - {"eligibility"}
    missing = required_keys - set(profile)
    if unknown:
        raise ValueError("profile contains unknown fields: " + ", ".join(sorted(unknown)))
    if missing:
        raise ValueError("profile is missing fields: " + ", ".join(sorted(missing)))
    if profile["version"] != 1:
        raise ValueError("profile.version must be 1")
    for name in ("sources", "digest"):
        if not isinstance(profile[name], dict):
            raise ValueError(f"profile.{name} must be an object")
    if "eligibility" in profile and not isinstance(profile["eligibility"], dict):
        raise ValueError("profile.eligibility must be an object")
    sources, digest = profile["sources"], profile["digest"]
    if set(sources) - SOURCE_KEYS:
        raise ValueError("profile.sources contains unknown fields: " + ", ".join(sorted(set(sources) - SOURCE_KEYS)))
    if set(digest) - DIGEST_KEYS:
        raise ValueError("profile.digest contains unknown fields: " + ", ".join(sorted(set(digest) - DIGEST_KEYS)))
    for name in ("domains", "primary", "exploration"):
        _string_list(profile, name)
    for name in ("venue_keys", "zotero_collection_keys"):
        if name in sources and (not isinstance(sources[name], list) or any(not isinstance(x, str) or not x for x in sources[name])):
            raise ValueError(f"profile.sources.{name} must be a list of non-empty strings")
    if "years" in sources and (not isinstance(sources["years"], list) or not sources["years"] or any(not isinstance(x, int) or isinstance(x, bool) for x in sources["years"])):
        raise ValueError("profile.sources.years must be a non-empty list of integers")
    for source_name in ("dblp", "arxiv", "ieee_xplore"):
        if source_name in sources and not isinstance(sources[source_name], dict):
            raise ValueError(f"profile.sources.{source_name} must be an object")
    if "quotas" in digest:
        quotas = digest["quotas"]
        if not isinstance(quotas, dict) or any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in quotas.values()):
            raise ValueError("profile.digest.quotas must be an object of non-negative integers")
    numeric_source_keys = {"recent_interest_items", "zotero_dedup_items", "openalex_enrich_limit"}
    for section, keys, label in ((sources, numeric_source_keys, "sources"), (digest, DIGEST_KEYS - {"quotas"}, "digest")):
        for key in keys & set(section):
            if not isinstance(section[key], (int, float)) or isinstance(section[key], bool):
                raise ValueError(f"profile.{label}.{key} must be numeric")
    return profile


def merge_profile(payload: dict[str, object], profile: dict[str, object]) -> None:
    profile_sources = dict(profile["sources"])
    adapter_sources = {name: profile_sources.pop(name) for name in ("dblp", "arxiv", "ieee_xplore") if name in profile_sources}
    payload.update(profile_sources)
    if adapter_sources:
        payload["sources"] = adapter_sources
    if "eligibility" in profile:
        allowed_eligibility = {"control_policy"}
        unknown = set(profile["eligibility"]) - allowed_eligibility
        if unknown:
            raise ValueError("profile.eligibility contains unknown fields: " + ", ".join(sorted(unknown)))
        payload["eligibility"] = dict(profile["eligibility"])
    payload["explicit_interests"] = list(profile["domains"])
    priority = payload.setdefault("topic_priority", {})
    priority["primary_topics"] = list(profile["primary"])
    priority["exploration_topics"] = list(profile["exploration"])
    digest = profile["digest"]
    if "min_score" in digest:
        payload["min_score"] = digest["min_score"]
    if "quotas" in digest:
        payload["digest"] = {"max_results": payload.get("max_results", 10), "quotas": dict(digest["quotas"])}
    for name in DIGEST_KEYS - {"min_score", "quotas"}:
        if name in digest:
            priority[name] = digest[name]
    topics = list(profile["primary"]) + list(profile["exploration"])
    for name in ("arxiv", "ieee_xplore"):
        source = payload.get("sources", {}).get(name) if isinstance(payload.get("sources"), dict) else None
        if isinstance(source, dict) and source.get("enabled"):
            source["topics"] = topics


def build(base: Path, output: Path, state_dir: Path, max_results: int, smtp_enabled: bool = True) -> None:
    if not 1 <= max_results <= 20:
        raise ValueError("max_results must be 1..20")
    required = ("SMTP_SENDER", "SMTP_RECEIVER", "LLM_BASE_URL", "LLM_MODEL")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError("missing Actions configuration: " + ", ".join(missing))
    payload = json.loads(base.read_text(encoding="utf-8"))
    profile_raw = os.environ.get("PAPER_SCOUT_PROFILE_JSON")
    if not profile_raw:
        raise RuntimeError("missing Actions configuration: PAPER_SCOUT_PROFILE_JSON")
    merge_profile(payload, load_profile(profile_raw))
    payload["max_results"] = max_results
    if isinstance(payload.get("digest"), dict):
        payload["digest"]["max_results"] = max_results
        quotas = payload["digest"].get("quotas", {})
        if sum(int(value) for value in quotas.values()) > max_results:
            raise ValueError("profile.digest.quotas exceed runtime max_results")
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
    parser.add_argument("--max-results", type=int, default=10)
    parser.add_argument("--disable-smtp", action="store_true")
    args = parser.parse_args()
    build(args.base, args.output, args.state_dir, args.max_results, not args.disable_smtp)
    print(f"Actions config generated at {args.output} (secrets remain environment-only)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
