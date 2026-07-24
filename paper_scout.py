#!/usr/bin/env python3
"""Low-resource Zotero -> DBLP -> CCF-A paper recommender (stdlib only)."""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import html
import http.client
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+#.-]{1,}|[\u4e00-\u9fff]{2,}")
STOP = {
    "the", "and", "for", "with", "from", "that", "this", "using", "based", "via", "into", "towards",
    "toward", "are", "is", "of", "to", "in", "on", "a", "an", "we", "our", "their", "paper", "method",
    "methods", "approach", "new", "study", "learning", "model", "models", "data", "analysis", "system",
}


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        if default is not None:
            return default
        raise FileNotFoundError(path)
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def open_json(req: str | urllib.request.Request, timeout: int = 30, attempts: int = 3) -> Any:
    """Open JSON with bounded retries for transient DNS/network failures."""
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            # 4xx responses are persistent request/authentication errors, not transient outages.
            if 400 <= exc.code < 500 and exc.code != 429:
                raise
            last = exc
            if attempt < attempts - 1:
                time.sleep(2 ** attempt)
        except (urllib.error.URLError, TimeoutError, http.client.RemoteDisconnected, json.JSONDecodeError) as exc:
            last = exc
            if attempt < attempts - 1:
                time.sleep(2 ** attempt)
    target = req.full_url if isinstance(req, urllib.request.Request) else req
    raise RuntimeError(f"request failed after {attempts} attempts: {target}: {last}")


def request_json(url: str, user_agent: str, timeout: int = 30) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "application/json"})
    return open_json(req, timeout=timeout)


def clean_text(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("text", "")
    return html.unescape(str(value or "")).replace("\n", " ").strip().rstrip(".")


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in TOKEN_RE.findall(text) if t.lower() not in STOP and len(t) > 1]


def fetch_zotero(config: dict[str, Any], user_agent: str) -> list[dict[str, str]]:
    user_id = os.environ.get("ZOTERO_USER_ID")
    api_key = os.environ.get("ZOTERO_API_KEY")
    if not user_id or not api_key:
        raise RuntimeError("ZOTERO_USER_ID and ZOTERO_API_KEY are required when --interests is not supplied")
    base = f"https://api.zotero.org/users/{urllib.parse.quote(user_id)}/items"
    params = {
        "format": "json", "limit": "100", "sort": "dateAdded", "direction": "desc",
        "itemType": "journalArticle || conferencePaper || preprint",
    }
    collections = config.get("zotero_collection_keys") or []
    urls: list[str] = []
    if collections:
        for key in collections:
            urls.append(f"https://api.zotero.org/users/{urllib.parse.quote(user_id)}/collections/{urllib.parse.quote(key)}/items?" + urllib.parse.urlencode(params))
    else:
        urls.append(base + "?" + urllib.parse.urlencode(params))
    headers = {"User-Agent": user_agent, "Zotero-API-Key": api_key, "Accept": "application/json"}
    papers: dict[str, dict[str, str]] = {}
    cap = int(config.get("recent_interest_items", 200))
    for initial in urls:
        start = 0
        while len(papers) < cap:
            url = initial + "&" + urllib.parse.urlencode({"start": start})
            req = urllib.request.Request(url, headers=headers)
            try:
                batch = open_json(req, timeout=30)
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    raise RuntimeError(
                        "Zotero refused access (HTTP 403/401). Verify that ZOTERO_USER_ID is the numeric "
                        "user ID shown at https://www.zotero.org/settings/security and that ZOTERO_API_KEY "
                        "belongs to that account with personal-library read access. If you changed the key, "
                        "export it again in this shell."
                    ) from None
                raise
            if not batch:
                break
            for item in batch:
                data = item.get("data", {})
                title = clean_text(data.get("title"))
                if title:
                    papers[item.get("key", title)] = {
                        "title": title,
                        "abstract": clean_text(data.get("abstractNote")),
                        "dateAdded": clean_text(data.get("dateAdded")),
                    }
            if len(batch) < 100:
                break
            start += len(batch)
    values = sorted(papers.values(), key=lambda p: p.get("dateAdded", ""), reverse=True)
    return values[:cap]


def fetch_dblp(venue: dict[str, Any], year: int, limit: int, user_agent: str) -> list[dict[str, Any]]:
    # Search syntax is public DBLP API syntax. We still verify every returned record key.
    query = f"venue:{venue['abbr'] or venue['dblp_key']}: year:{year}:"
    params = urllib.parse.urlencode({"q": query, "h": limit, "format": "json"})
    endpoints = (
        "https://dblp.org/search/publ/api?" + params,
        "https://dblp.uni-trier.de/search/publ/api?" + params,
    )
    errors: list[str] = []
    for url in endpoints:
        try:
            payload = request_json(url, user_agent)
            break
        except RuntimeError as exc:
            errors.append(str(exc))
    else:
        raise RuntimeError("all DBLP endpoints failed: " + " | ".join(errors))
    hits = payload.get("result", {}).get("hits", {}).get("hit", [])
    if isinstance(hits, dict):
        hits = [hits]
    result = []
    prefix = ("conf/" if venue["type"] == "conference" else "journals/") + venue["dblp_key"] + "/"
    for hit in hits:
        info = hit.get("info", {})
        key = clean_text(info.get("key"))
        # Hard quality gate: text match alone cannot pass.
        if not key.startswith(prefix):
            continue
        authors = info.get("authors", {}).get("author", [])
        if isinstance(authors, (str, dict)):
            authors = [authors]
        result.append({
            "id": key,
            "title": clean_text(info.get("title")),
            "authors": [clean_text(a) for a in authors],
            "year": int(clean_text(info.get("year")) or year),
            "venue": venue["abbr"] or clean_text(info.get("venue")),
            "venue_name": venue["name"],
            "rank": venue["rank"],
            "type": venue["type"],
            "url": clean_text(info.get("url")) or f"https://dblp.org/rec/{key}",
            "ee": clean_text(info.get("ee")),
        })
    return result


def openalex_abstract(inverted: Any) -> str:
    if not isinstance(inverted, dict):
        return ""
    positions: list[tuple[int, str]] = []
    for word, indexes in inverted.items():
        for index in indexes:
            positions.append((int(index), word))
    return " ".join(word for _, word in sorted(positions))


def enrich_candidates(candidates: list[dict[str, Any]], limit: int, user_agent: str) -> None:
    """Best-effort DOI -> OpenAlex abstract enrichment; quality still comes from DBLP."""
    enriched = 0
    for paper in candidates:
        if enriched >= limit:
            break
        doi_match = re.search(r"(?:doi\.org/|doi:)(10\.[^\s?#]+)", paper.get("ee", ""), re.I)
        if not doi_match:
            continue
        doi = doi_match.group(1).rstrip(".,)")
        url = "https://api.openalex.org/works/" + urllib.parse.quote("https://doi.org/" + doi, safe="")
        try:
            payload = request_json(url, user_agent)
            paper["abstract"] = openalex_abstract(payload.get("abstract_inverted_index"))
            paper["openalex_id"] = payload.get("id", "")
            enriched += 1
        except RuntimeError:
            paper["abstract"] = ""


def rank_candidates(interests: list[dict[str, str]], candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    interest_docs = [tokenize((p.get("title", "") + " ") * 3 + p.get("abstract", "")) for p in interests]
    if not any(interest_docs):
        raise RuntimeError("interest corpus has no usable title/abstract text")
    df: collections.Counter[str] = collections.Counter()
    for doc in interest_docs:
        df.update(set(doc))
    n = len(interest_docs)
    profile: collections.Counter[str] = collections.Counter()
    # Recency by Zotero order: recent documents get a modest boost, not a monopoly.
    for index, doc in enumerate(interest_docs):
        recency = math.exp(-index / max(20.0, n / 3))
        counts = collections.Counter(doc)
        for term, tf in counts.items():
            idf = math.log((n + 1) / (df[term] + 0.5)) + 1.0
            profile[term] += (1 + math.log(tf)) * idf * (0.5 + 0.5 * recency)
    norm = math.sqrt(sum(v * v for v in profile.values())) or 1.0
    for paper in candidates:
        candidate_text = (paper["title"] + " ") * 3 + paper.get("abstract", "")
        terms = tokenize(candidate_text)
        counts = collections.Counter(terms)
        contributions: dict[str, float] = {}
        score = 0.0
        for term, tf in counts.items():
            value = profile.get(term, 0.0) * (1 + math.log(tf)) / norm
            if value:
                contributions[term] = value
                score += value
        # Exact title n-gram overlap is especially meaningful when candidate abstracts are absent.
        title_lower = paper["title"].lower()
        for phrase in ("retrieval augmented", "language model", "computer vision", "reinforcement learning"):
            if phrase in title_lower and any(phrase in (x.get("title", "") + " " + x.get("abstract", "")).lower() for x in interests):
                score += 0.12
        paper["score"] = score
        paper["reasons"] = [k for k, _ in sorted(contributions.items(), key=lambda kv: kv[1], reverse=True)[:6]]
    return sorted(candidates, key=lambda x: (x["score"], x["year"], x["title"]), reverse=True)


def render_report(papers: list[dict[str, Any]], interest_count: int, candidate_count: int) -> str:
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    lines = ["# CCF-A 兴趣论文推荐", "", f"生成时间：{now}", f"兴趣库：{interest_count} 篇；CCF-A 候选：{candidate_count} 篇；本次输出：{len(papers)} 篇。", ""]
    if not papers:
        lines += ["本次没有未推荐过且通过 CCF-A 硬过滤的候选论文。", ""]
    for i, p in enumerate(papers, 1):
        reasons = "、".join(p["reasons"]) if p["reasons"] else "弱匹配（可扩大兴趣集合或接入摘要补全）"
        authors = ", ".join(p["authors"][:8]) + (" et al." if len(p["authors"]) > 8 else "")
        lines += [
            f"## {i}. {p['title']}", "",
            f"- 相关度：{p['score']:.4f}",
            f"- Venue：{p['venue']}（CCF-{p['rank']}，{p['year']}，{p['type']}）",
            f"- 作者：{authors}",
            f"- 匹配依据：{reasons}",
            f"- DBLP：{p['url']}",
        ]
        if p.get("ee"):
            lines.append(f"- 出版/全文入口：{p['ee']}")
        lines.append("")
    lines += ["---", "质量说明：所有结果均通过本地 CCF-A 白名单及 DBLP record-key 双重校验；CCF 等级仍应定期对照官方目录更新。", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--interests", type=Path, help="local JSON list; otherwise use Zotero Web API")
    parser.add_argument("--venues", type=Path, default=Path(__file__).parent / "data/ccf_a_venues.json")
    parser.add_argument("--output", type=Path, default=Path("recommendations.md"))
    parser.add_argument("--no-update-seen", action="store_true")
    args = parser.parse_args()
    config = load_json(args.config)
    user_agent = config.get("user_agent", "ccf-paper-scout/0.1")
    interests = load_json(args.interests) if args.interests else fetch_zotero(config, user_agent)
    venue_data = load_json(args.venues)
    venue_by_key = {v["dblp_key"].lower(): v for v in venue_data["venues"] if v["rank"] == "A"}
    requested = [x.lower() for x in config.get("venue_keys", [])]
    unknown = sorted(set(requested) - set(venue_by_key))
    if unknown:
        raise RuntimeError(f"venue_keys are absent from CCF-A whitelist: {', '.join(unknown)}")
    seen_path = Path(config.get("seen_db", "state/seen.json"))
    if not seen_path.is_absolute():
        seen_path = args.config.resolve().parent / seen_path
    seen = set(load_json(seen_path, {"ids": []}).get("ids", []))
    candidates: dict[str, dict[str, Any]] = {}
    for key in requested:
        for year in config.get("years", [dt.date.today().year]):
            papers = fetch_dblp(venue_by_key[key], int(year), int(config.get("per_venue", 30)), user_agent)
            for paper in papers:
                if paper["id"] not in seen:
                    candidates[paper["id"]] = paper
            time.sleep(float(config.get("request_delay_seconds", 1.0)))
    candidate_values = list(candidates.values())
    enrich_candidates(candidate_values, int(config.get("openalex_enrich_limit", 20)), user_agent)
    ranked = rank_candidates(interests, candidate_values)
    min_score = float(config.get("min_score", 0.01))
    ranked = [paper for paper in ranked if paper["score"] >= min_score]
    selected = ranked[: int(config.get("max_results", 20))]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_report(selected, len(interests), len(candidates)), encoding="utf-8")
    if not args.no_update_seen and selected:
        seen.update(p["id"] for p in selected)
        seen_path.parent.mkdir(parents=True, exist_ok=True)
        seen_path.write_text(json.dumps({"ids": sorted(seen)}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(selected)} recommendations from {len(candidates)} candidates to {args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, FileNotFoundError, urllib.error.URLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
