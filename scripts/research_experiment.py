"""Run a public, bounded research-profile experiment locally or in GitHub Actions."""
from __future__ import annotations

import argparse
import collections
import copy
import datetime as dt
import html
import os
from pathlib import Path
import time
import urllib.parse
import json

import paper_scout
from state_store import StateStore
from ccf_paper_scout import benchmark as bench, official_abstracts, proceedings, semantic

ROOT = Path(__file__).resolve().parents[1]
FAMILIES = {
    "world_models": r"world model|model.based reinforcement|latent dynamics|learned dynamics|imagination",
    "self_play": r"self.play|league training|population.based training|open.ended learning|competitive.*reinforcement",
    "action_policies": r"hierarchical reinforcement|goal.conditioned|imitation learning|decision transformer|vision.language.*agent|reward design",
}
VENUES = {"aaai": "AAAI", "acl": "ACL", "icml": "ICML", "nips": "NeurIPS"}


def query_text(venue: str, pattern: str) -> str:
    if venue not in VENUES:
        raise ValueError("unsupported venue")
    return '''PREFIX d: <https://dblp.org/rdf/schema#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
SELECT ?publication ?title ?year ?doi ?document ?proceedings
       (GROUP_CONCAT(DISTINCT ?authorName; separator="|||") AS ?authors)
WHERE {
 ?publication a d:Inproceedings; d:title ?title; d:yearOfPublication ?year;
 d:publishedInStream <https://dblp.org/streams/conf/%s> .
 FILTER(?year = "2025"^^xsd:gYear)
 FILTER(REGEX(STR(?title), %s, "i"))
 OPTIONAL { ?publication d:doi ?doi }
 OPTIONAL { ?publication d:primaryDocumentPage ?document }
 OPTIONAL { ?publication d:publishedAsPartOf ?proceedings }
 OPTIONAL { ?publication d:authoredBy ?author . ?author d:primaryCreatorName ?authorName }
}
GROUP BY ?publication ?title ?year ?doi ?document ?proceedings
ORDER BY ?publication
LIMIT 50''' % (venue, json.dumps(pattern))


def discover(output: Path) -> dict:
    rows, audits = {}, []
    catalog = proceedings.registry()
    for family, pattern in FAMILIES.items():
        for venue, name in VENUES.items():
            query = query_text(venue, pattern)
            url = paper_scout.DBLP_SPARQL_ENDPOINT + "?" + urllib.parse.urlencode({"query": query, "format": "json"})
            audit = {"family": family, "venue": venue, "query": query, "url": url}
            try:
                payload = paper_scout.request_dblp_json(url, "ccf-paper-scout/0.2 (bounded research experiment)", 30, 1, 3,
                                                       "application/sparql-results+json")
                bindings = payload["results"]["bindings"]
                if not isinstance(bindings, list):
                    raise ValueError("invalid SPARQL bindings")
                for b in bindings:
                    values = {k: v["value"] for k, v in b.items()}
                    identifier = values["publication"].removeprefix("https://dblp.org/rec/")
                    if not identifier.startswith(f"conf/{venue}/") or values["year"] != "2025":
                        raise ValueError("query returned wrong venue/year")
                    p = {"id": identifier, "title": paper_scout.clean_text(values["title"]),
                         "authors": [paper_scout.clean_text(a) for a in values.get("authors", "").split("|||") if a],
                         "year": 2025, "venue": name, "venue_name": name, "rank": "A", "type": "conference",
                         "url": values["publication"], "ee": values.get("document", ""),
                         "doi": paper_scout.normalize_doi(values.get("doi", "")),
                         "proceedings_id": values.get("proceedings", ""), "historical_import": True,
                         "query_families": []}
                    proceedings.qualify(p, catalog)
                    rows.setdefault(identifier, p)
                    if family not in rows[identifier]["query_families"]:
                        rows[identifier]["query_families"].append(family)
                audit.update(status="success", bindings=len(bindings), possibly_truncated=len(bindings) >= 50)
            except (RuntimeError, OSError, ValueError, KeyError) as exc:
                audit.update(status="failed", error_type=type(exc).__name__, reason=str(exc)[:250])
            audits.append(audit)
            bench.write_json(output / "discovery.json", {"papers": list(rows.values()), "queries": audits})
            print(f"{family}/{venue}: {audit['status']} {audit.get('bindings', '')}", flush=True)
    if any(a["status"] != "success" for a in audits):
        raise RuntimeError("discovery incomplete; inspect discovery.json before retrying with a new output directory")
    return {"papers": list(rows.values()), "queries": audits}


def acquisition_order(papers: list[dict]) -> list[dict]:
    queues = [collections.deque(sorted((p for p in papers if family in p["query_families"] and
               p["id"].startswith(f"conf/{venue}/")), key=lambda p: p["id"])) for family in FAMILIES for venue in VENUES]
    result, seen = [], set()
    while any(queues):
        for queue in queues:
            while queue and queue[0]["id"] in seen:
                queue.popleft()
            if queue:
                p = queue.popleft()
                result.append(p)
                seen.add(p["id"])
    return result


def enrich(papers: list[dict], output: Path, budget: int, fetch=bench.fetch, delay: float = 1) -> list[dict]:
    attempts, blocked = [], set()
    for p in acquisition_order(papers):
        p["abstract"] = ""
        url = official_abstracts.official_url(p)
        p["metadata_status"] = "budget_not_covered" if url else "unsupported_official_source"
        if not url or len(attempts) >= budget:
            continue
        host = urllib.parse.urlsplit(url).hostname
        if host in blocked:
            p["metadata_status"] = "source_throttled"
            continue
        attempt = {"id": p["id"], "url": url}
        try:
            abstract = official_abstracts.parse_abstract(fetch(url), p)
            p.update(abstract=abstract, abstract_source="official", abstract_source_url=url,
                     abstract_hash=bench.digest(abstract))
            attempt["status"] = p["metadata_status"] = "success"
        except (OSError, ValueError, RuntimeError) as exc:
            attempt.update(status="failed", error_type=type(exc).__name__, http_status=getattr(exc, "code", None), reason=str(exc)[:180])
            p["metadata_status"] = "failed"
            if attempt["http_status"] == 429:
                blocked.add(host)
        attempts.append(attempt)
        bench.write_json(output / "abstract-attempts.json", attempts)
        bench.write_json(output / "metadata-checkpoint.json", {"papers": papers})
        if len(attempts) % 10 == 0:
            print(f"Abstract requests: {len(attempts)}/{budget}", flush=True)
        time.sleep(delay)
    return attempts


def research_config(example: dict, profile: dict) -> dict:
    config = bench.config_for_experiment(example)
    for key in ("explicit_interests", "research_profile", "topic_priority"):
        config[key] = copy.deepcopy(profile[key])
    return config


def report(output: Path, results: dict, counts: dict) -> None:
    e = html.escape
    parts = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Research profile comparison</title><style>body{font:16px/1.7 system-ui;max-width:1100px;margin:30px auto;padding:0 20px}details{border:1px solid #ccd;padding:14px;margin:12px 0}summary{cursor:pointer}a{color:#1859a5}</style><h1>新画像定向检索实验</h1><p>原画像和新画像共享同一批新采集的、通过主会核验且有官方摘要的论文。词项、语义和 RRF 排序均为 shadow；没有人工标签，不报告准确率。RL/VLM 等主题相似仍可能混淆环境行动与通用回答优化。</p>',
             f'<p>检索 {counts["discovered"]} 条；主会核验 {counts["qualified"]} 条；有摘要并去重后 {counts["ranked"]} 条；摘要请求 {counts["requests"]} 次。</p>']
    for profile, result in results.items():
        lookup = {p["id"]: p for p in result["papers"]}
        parts.append(f'<h2>{e(profile)}</h2>')
        for method, ranking in result["rankings"].items():
            parts.append(f'<h3>{e(method)} Top 20</h3>')
            for rank, identifier in enumerate(ranking[:20], 1):
                p = lookup[identifier]
                parts.append(f'<details><summary>#{rank} {e(p["title"])}</summary><p>{e(p["venue"])} {p["year"]} · <a href="{e(p["abstract_source_url"], quote=True)}">官方论文页</a></p><p>{e(p["abstract"])}</p></details>')
    parts.append('</html>')
    (output / "results.html").write_text("\n".join(parts), encoding="utf-8")


def run(output: Path, profile_path: Path, budget: int) -> None:
    if not 1 <= budget <= 100:
        raise ValueError("abstract budget must be 1..100")
    output.mkdir(parents=True, exist_ok=False)
    source = discover(output)
    papers = [p for p in source["papers"] if p["main_conference_status"] == "verified"]
    attempts = enrich(papers, output, budget)
    pool, identities = [], set()
    for p in sorted(papers, key=lambda p: p["id"]):
        keys = {"title:" + official_abstracts.normalized(p["title"])}
        if p.get("doi"):
            keys.add("doi:" + p["doi"].lower())
        if p.get("abstract") and not keys.intersection(identities):
            pool.append(p)
            identities.update(keys)
    if not pool:
        raise RuntimeError("no verified official abstracts; inspect acquisition artifacts")
    example = bench.read_json(ROOT / "config.example.json")
    configs = {"baseline": bench.config_for_experiment(example), "research": research_config(example, bench.read_json(profile_path))}
    counts = {"discovered": len(source["papers"]), "qualified": len(papers), "ranked": len(pool), "requests": len(attempts),
              "abstracts_by_venue": dict(collections.Counter(p["venue"] for p in pool))}
    snapshot = {"created_at": dt.datetime.now(dt.timezone.utc).isoformat(), "papers": pool, "configs": configs,
                "counts": counts, "scope": "2025 title-filtered public discovery, 12 queries with limit 50 each; not full-corpus recall"}
    snapshot["dataset_id"] = bench.digest(snapshot)
    bench.write_json(output / "snapshot.json", snapshot)
    os.environ.setdefault("HF_HOME", str(ROOT / ".model-cache"))
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    encoder = semantic.Encoder(configs["research"]["semantic"])
    store = StateStore(output / "vectors.sqlite3")
    results = {}
    try:
        for name, config in configs.items():
            results[name] = bench.compare(pool, config, store, encoder=encoder)
            bench.write_json(output / f"{name}.json", results[name])
        replay = bench.compare(pool, configs["research"], store, encoder=encoder)
        if replay["rankings"] != results["research"]["rankings"]:
            raise RuntimeError("offline replay changed rankings")
    finally:
        store.close()
    report(output, results, counts)
    bench.write_json(output / "validation.json", {"counts": counts, "replay_identical": True,
                     "replay_encoded": replay["semantic"]["encoded"], "independent_metrics": None})
    print(f"Research experiment complete: {counts}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", type=Path, default=ROOT / "profiles/rl-action-world-models.json")
    parser.add_argument("--abstract-budget", type=int, default=80)
    args = parser.parse_args()
    run(args.output, args.profile, args.abstract_budget)
