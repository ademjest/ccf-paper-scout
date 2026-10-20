"""Budgeted, resumable DBLP discovery, independent of recommendation history."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def resolve_years(config: dict[str, Any], today: dt.date | None = None) -> list[int]:
    policy = config.get("year_policy", {})
    mode = policy.get("mode", "fixed" if config.get("years") else "rolling")
    if mode == "fixed":
        years = config.get("years", [])
        if not isinstance(years, list) or not years:
            raise ValueError("fixed year policy requires a non-empty years list")
        if any(isinstance(year, bool) or not isinstance(year, int) or not 1900 <= year <= 2200 for year in years):
            raise ValueError("years must contain integer calendar years")
        return sorted(set(years), reverse=True)
    if mode != "rolling":
        raise ValueError("year_policy.mode must be fixed or rolling")
    if config.get("years"):
        raise ValueError("rolling year_policy and explicit years are mutually exclusive")
    try:
        timezone = ZoneInfo(policy.get("timezone", "Asia/Shanghai"))
    except ZoneInfoNotFoundError as exc:
        raise ValueError("unknown year_policy timezone") from exc
    today = today or dt.datetime.now(timezone).date()
    lookback = int(policy.get("lookback_years", 1))
    if not 0 <= lookback <= 10:
        raise ValueError("lookback_years must be between 0 and 10")
    return list(range(today.year, today.year - lookback - 1, -1))


def discover(store: Any, venues: dict[str, dict[str, Any]], years: list[int], config: dict[str, Any],
             user_agent: str, fetch_page: Callable, *, probe_year: int | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    page_size = int(config.get("page_size", 100))
    budget = int(config.get("max_pages_per_run", 80))
    per_partition = int(config.get("max_pages_per_partition_per_run", config.get("max_pages_per_venue", 12)))
    if not 1 <= page_size <= 1000 or budget < 1 or per_partition < 1:
        raise ValueError("invalid DBLP page or request budget")
    if int(config.get("overlap_pages", 1)) not in (0, 1):
        raise ValueError("overlap_pages currently supports 0 or 1")
    if not 0 <= float(config.get("minimum_success_ratio", 0.75)) <= 1:
        raise ValueError("minimum_success_ratio must be between 0 and 1")
    if config.get("failure_policy", "continue") not in ("strict", "continue"):
        raise ValueError("failure_policy must be strict or continue")
    if any(key in config for key in ("target_unseen_per_venue", "stop_after_seen_pages")):
        print("warning: legacy unseen/seen-page stop parameters are ignored; discovery is budgeted and persistent")
    # Pin a transport per scan. SPARQL avoids Search API's cloud anti-bot path and unstable result ordering.
    transport = config.get("transport", "sparql")
    if transport not in ("sparql", "search"):
        raise ValueError("dblp.transport must be sparql or search")
    stats = {"pages": 0, "new_papers": 0, "completed": 0, "partial": 0, "attempted": 0, "overdue": 0}
    failures: list[dict[str, Any]] = []
    current = dt.datetime.now(dt.timezone.utc)
    partitions = []
    for key, venue in venues.items():
        for year in [*years, *([probe_year] if probe_year is not None and probe_year not in years else [])]:
            state = store.scan_state(key, year)
            if state.get("next_check_at", "") > current.isoformat():
                continue
            partitions.append((key, venue, year, state))
    # Oldest checked first; reserve at least one request per partition when the budget permits.
    partitions.sort(key=lambda item: (item[3].get("last_checked_at", ""), -item[2], item[0]))
    active_attempts = active_failures = 0
    for index, (key, venue, year, state) in enumerate(partitions):
        if stats["pages"] >= budget:
            break
        allocation = min(per_partition, max(1, (budget - stats["pages"]) // (len(partitions) - index)))
        signature = hashlib.sha256(json.dumps([key, year, page_size, transport, "ordered-v1"]).encode()).hexdigest()
        if state.get("signature") != signature:
            state = {"initialized": state.get("initialized", False), "signature": signature, "next_offset": 0}
        if state.get("status") in ("complete", "not_indexed_yet"):
            state["next_offset"] = 0
            state.pop("cycle_started_at", None)
        state.setdefault("cycle_started_at", current.isoformat())
        if (current - dt.datetime.fromisoformat(state["cycle_started_at"])).days >= int(config.get("full_cycle_target_days", 7)):
            stats["overdue"] += 1
        stats["attempted"] += 1
        is_probe = year == probe_year and year not in years and not (state.get("initialized") or state.get("has_records"))
        active_attempts += not is_probe
        offset = int(state.get("next_offset", 0))
        # Replay one boundary page without consuming the entire run on that same page.
        offsets = [offset]
        overlap = min(int(config.get("overlap_pages", 1)), offset // page_size)
        if overlap and allocation >= 2:
            offsets = [offset - page_size, offset]
        if offset > page_size and allocation >= 3:
            offsets = [0, *offsets]
        try:
            for page_index in range(allocation):
                start = offsets[page_index] if page_index < len(offsets) else offsets[-1] + (page_index - len(offsets) + 1) * page_size
                if transport == "search" and start >= int(config.get("search_max_offset", 1000)):
                    raise RuntimeError("Search pagination bound reached; switch dblp.transport to sparql")
                stats["pages"] += 1
                page, total, raw = fetch_page(venue, year, page_size, start, user_agent, {**config, "transport": transport})
                done = raw < page_size or (total is not None and start + raw >= total)
                # Save the old initialization flag with the page, so bootstrap records remain historical.
                next_state = {**state, "signature": signature, "transport": transport, "total": total,
                              "next_offset": max(offset, start + raw), "status": "complete" if done else "partial",
                              "has_records": state.get("has_records", False) or bool(raw),
                              "last_error": "", "next_check_at": ""}
                stats["new_papers"] += store.save_discovery_page(key, year, page, next_state)
                state = next_state
                if done:
                    state["initialized"] = state.get("initialized", False) or bool(start + raw)
                    state["completed_at"] = current.isoformat()
                    state["status"] = "not_indexed_yet" if start + raw == 0 else "complete"
                    interval = int(config.get("empty_recheck_days", 7)) if not state["initialized"] else int(config.get("refresh_days", 1))
                    state["next_check_at"] = (current + dt.timedelta(days=interval)).isoformat()
                    store.save_discovery_page(key, year, [], state)
                    stats["completed"] += 1
                    break
            else:
                stats["partial"] += 1
        except RuntimeError as exc:
            state["last_error"] = str(exc)
            store.save_discovery_page(key, year, [], state)
            failures.append({"venue": venue.get("abbr", key), "venue_key": key, "year": year, "error": str(exc)})
            active_failures += not is_probe
            if (getattr(exc, "global_failure", False) or config.get("failure_policy") == "strict" or
                    (active_failures >= 2 and active_failures == active_attempts)):
                raise RuntimeError("DBLP source unavailable; discovery cursor retained") from exc
        print(f"source=DBLP venue={venue.get('abbr', key)} year={year} status={state.get('status', 'error')} offset={state.get('next_offset', 0)}")
    ratio = (active_attempts - active_failures) / active_attempts if active_attempts else 1.0
    stats["success_ratio"] = ratio
    if ratio < float(config.get("minimum_success_ratio", 0.75)):
        raise RuntimeError(f"DBLP success ratio {ratio:.3f} below configured minimum; source_unavailable")
    if stats["overdue"]:
        print(f"warning: discovery overdue_cycles={stats['overdue']}; increase budget or reduce partitions")
    return stats, failures
