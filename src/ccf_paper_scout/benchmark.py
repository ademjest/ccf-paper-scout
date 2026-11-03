"""Small, isolated paper-ranking experiment; never sends mail or loads private profiles."""
from __future__ import annotations

import collections
import copy
import hashlib
import json
import math
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import official_abstracts, semantic


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def config_for_experiment(example: dict) -> dict:
    # Allow-list sections: do not serialize credentials or arbitrary user configuration.
    config = {k: copy.deepcopy(example[k]) for k in (
        "explicit_interests", "topic_priority", "research_profile", "selection", "semantic", "main_conference")}
    config.update(years=[2025], venue_keys=["aaai", "acl", "icml", "nips"], max_results=5)
    config["semantic"].update(enabled=True, mode="shadow", max_updated_papers_per_run=2000)
    config["relation_policy"] = {"enabled": True, "mode": "shadow", "review": {"enabled": False}}
    config["delivery"] = {"smtp": {"enabled": False}}
    config["llm_translation"] = {"enabled": False}
    return config


def venue(paper: dict) -> str:
    return paper["id"].split("/")[1]


def rank_ids(papers: list[dict], field: str) -> list[str]:
    return [p["id"] for p in sorted(papers, key=lambda p: (-p[field], p["id"]))]


def acquisition_order(papers: list[dict], seed: int = 20261103) -> list[dict]:
    """Round-robin venues; alternate lexical/semantic candidates and 20% exploration."""
    rng = random.Random(seed)
    queues = {}
    for key in sorted({venue(p) for p in papers}):
        group = [p for p in papers if venue(p) == key]
        lexical = sorted(group, key=lambda p: (-p["lexical_score"], p["id"]))
        dense = sorted(group, key=lambda p: (-p["semantic_score"], p["id"]))
        random_order = sorted(group, key=lambda p: p["id"])
        rng.shuffle(random_order)
        queues[key] = [collections.deque(rows) for rows in (lexical, dense, random_order)]
    result, seen = [], set()
    for round_index in range(len(papers)):
        for queue in queues.values():
            chosen = queue[2 if round_index % 5 == 4 else round_index % 2]
            while chosen and chosen[0]["id"] in seen:
                chosen.popleft()
            if chosen:
                paper = chosen.popleft()
                seen.add(paper["id"])
                result.append(paper)
        if len(seen) == len(papers):
            break
    return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(url: str) -> str:
    # One invocation = one HTTP request. Redirects and retries cannot exceed the budget silently.
    request = urllib.request.Request(url, headers={"User-Agent": "ccf-paper-scout/0.2 (bounded offline evaluation dataset)"})
    with urllib.request.build_opener(NoRedirect).open(request, timeout=20) as response:
        body = response.read(2_000_001)
        if len(body) > 2_000_000:
            raise ValueError("response_too_large")
        return body.decode("utf-8")


def enrich(papers: list[dict], budget: int, fetch_text=fetch, checkpoint=None, delay: float = 1.0) -> dict:
    """Experimental metadata path. No private API keys; full per-paper provenance."""
    import paper_scout

    stats = {"requests": 0, "by_source": collections.Counter()}
    blocked, last_request = set(), {}
    for paper in papers:
        paper["metadata_audit"] = {"status": "existing" if paper.get("abstract") else "not_attempted_budget", "attempts": []}
    for paper in acquisition_order(papers):
        audit = paper["metadata_audit"]
        if paper.get("abstract"):
            continue
        doi = paper_scout.normalize_doi(paper.get("doi", "")) or paper_scout.normalize_doi(paper.get("ee", ""))
        official = official_abstracts.official_url(paper)
        routes = []
        if official:
            routes.append(("official", official))
        if doi:
            routes.append(("openalex", "https://api.openalex.org/works/" + urllib.parse.quote("https://doi.org/" + doi, safe="")))
        if not routes:
            audit["status"] = "no_supported_identifier"
        for source, url in routes:
            host = urllib.parse.urlsplit(url).hostname
            if host in blocked:
                if not audit["attempts"]:
                    audit["status"] = "source_throttled"
                continue
            if stats["requests"] >= budget:
                break
            time.sleep(max(0, delay - (time.monotonic() - last_request.get(host, 0))))
            stats["requests"] += 1
            stats["by_source"][host] += 1
            attempt = {"source": source, "url": url}
            audit["attempts"].append(attempt)
            try:
                document = fetch_text(url)
                if source == "official":
                    abstract = official_abstracts.parse_abstract(document, paper)
                else:
                    payload = json.loads(document)
                    if paper_scout.normalize_doi(payload.get("doi", "")) != doi:
                        raise ValueError("openalex_doi_mismatch")
                    abstract = paper_scout.openalex_abstract(payload.get("abstract_inverted_index"))
                audit["status"] = attempt["status"] = "success" if abstract else "missing"
                if abstract:
                    paper.update(abstract=abstract, abstract_source=source, abstract_source_url=url,
                                 abstract_hash=digest(abstract))
            except (OSError, ValueError, RuntimeError) as exc:
                code = getattr(exc, "code", None)
                attempt.update(status="failed", error_type=type(exc).__name__, http_status=code)
                if isinstance(exc, ValueError):
                    attempt["reason"] = str(exc)[:100]
                audit["status"] = "failed"
                if code == 429:
                    blocked.add(host)
            finally:
                last_request[host] = time.monotonic()
                if checkpoint:
                    checkpoint(papers, stats)
            if paper.get("abstract"):
                break
    stats["statuses"] = dict(collections.Counter(p["metadata_audit"]["status"] for p in papers))
    stats["by_venue"] = {}
    for key in sorted({venue(p) for p in papers}):
        group = [p for p in papers if venue(p) == key]
        stats["by_venue"][key] = {"candidates": len(group), "abstracts": sum(bool(p.get("abstract")) for p in group),
                                  "statuses": dict(collections.Counter(p["metadata_audit"]["status"] for p in group))}
    return stats


def compare(papers: list[dict], config: dict, store, encoder=None) -> dict:
    import paper_scout
    from . import relations, selection

    papers = copy.deepcopy(papers)
    ranked = paper_scout.rank_candidates([], papers, config["explicit_interests"])
    ranked = paper_scout.apply_topic_priorities(ranked, config["topic_priority"])
    config = copy.deepcopy(config)
    config["semantic"]["max_updated_papers_per_run"] = len(papers)
    stats = semantic.rank(ranked, [], config, store, encoder=encoder)
    if stats["deferred"]:
        raise ValueError("incomplete semantic coverage")
    relations.assess(ranked, config, store, "offline-benchmark", None, None, allow_network=False)
    baseline = selection.select(ranked, 10, config)
    # Scoring comparison is over the same abstract-bearing, verified paper set.
    evidence = [p for p in ranked if p.get("abstract") and p.get("main_conference_status") == "verified"]
    lexical, dense = rank_ids(evidence, "lexical_score"), rank_ids(evidence, "semantic_score")
    rrf = collections.Counter()
    for ids in (lexical, dense):
        for rank, identifier in enumerate(ids, 1):
            rrf[identifier] += 1 / (60 + rank)
    fused = sorted(rrf, key=lambda identifier: (-rrf[identifier], identifier))
    return {"papers": sorted(ranked, key=lambda p: p["id"]), "semantic": stats,
            "rankings": {"lexical": lexical, "semantic": dense, "rrf": fused},
            "baseline_selected_ids": [p["id"] for p in baseline],
            "comparison_scope": "raw rankings of verified abstract-bearing candidates; baseline alone applies existing topic gates and quotas"}


def sample_tasks(result: dict, size: int = 80, seed: int = 20261103) -> dict:
    rng = random.Random(seed)
    papers = {p["id"]: p for p in result["papers"] if p.get("abstract")}
    selected = set()
    for ranking in result["rankings"].values():
        selected.update(ranking[:20])
    # If the union is larger than requested, retain it instead of truncating one method's top results.
    for key in sorted({venue(p) for p in papers.values()}):
        choices = sorted(p["id"] for p in papers.values() if venue(p) == key and p["id"] not in selected)
        rng.shuffle(choices)
        selected.update(choices[:5])
    remainder = sorted(set(papers) - selected)
    rng.shuffle(remainder)
    selected.update(remainder[:max(0, size - len(selected))])
    # Join exact-title or DOI duplicates before assigning folds, including transitive links.
    families, keys = {}, {}
    for identifier in sorted(selected):
        p = papers[identifier]
        identity_keys = ["title:" + official_abstracts.normalized(p["title"])]
        if p.get("doi"):
            identity_keys.append("doi:" + p["doi"].lower())
        matches = {keys[k] for k in identity_keys if k in keys}
        family = min(matches | {identifier})
        members = {identifier}
        for old in matches:
            members.update(families.pop(old))
            for k in keys:
                if keys[k] == old:
                    keys[k] = family
        families[family] = members
        for key in identity_keys:
            keys[key] = family
    folds = {}
    for key in sorted({venue(p) for p in papers.values()}):
        groups = sorted(f for f in families if venue(papers[f]) == key)
        rng.shuffle(groups)
        for index, family in enumerate(groups):
            for identifier in families[family]:
                folds[identifier] = "test" if index % 4 == 0 else "calibration"
    identifiers = sorted(selected)
    rng.shuffle(identifiers)
    return {"seed": seed, "folds": folds, "tasks": [{k: papers[i].get(k, "") for k in
            ("id", "title", "authors", "year", "venue", "abstract", "url", "ee", "abstract_source_url")} for i in identifiers]}


def evaluate(result: dict, manifest: dict, labels: dict, fold: str) -> dict:
    if labels.get("dataset_id") != result["dataset_id"] or manifest["dataset_id"] != result["dataset_id"]:
        raise ValueError("labels/manifest belong to a different dataset")
    rows = labels.get("labels", [])
    if len({r["id"] for r in rows}) != len(rows):
        raise ValueError("duplicate labels")
    papers = {p["id"]: p for p in result["papers"]}
    grades = {}
    for row in rows:
        if row["id"] not in manifest["folds"]:
            raise ValueError("unknown label ID")
        grade = row.get("grade")
        if grade is not None and (type(grade) is not int or grade not in (0, 1, 2)):
            raise ValueError("grade must be 0, 1, 2 or null")
        if grade is not None:
            evidence = row.get("evidence", "").strip()
            if not evidence or evidence not in papers[row["id"]]["abstract"]:
                raise ValueError("each judgement needs an exact abstract excerpt")
            if not row.get("reason", "").strip():
                raise ValueError("each judgement needs a reason")
            grades[row["id"]] = grade
    pool = {i for i, split in manifest["folds"].items() if split == fold}

    def metrics(ids):
        ids = [i for i in ids if i in pool]
        answer = {"pool_size": len(ids), "judged": sum(i in grades for i in ids)}
        for k in (5, 10):
            top = ids[:k]
            complete = len(top) == k and all(i in grades for i in top)
            answer[f"judged@{k}"] = sum(i in grades for i in top)
            answer[f"precision@{k}"] = sum(grades[i] >= 1 for i in top) / k if complete else None
            answer[f"strict_precision@{k}"] = sum(grades[i] == 2 for i in top) / k if complete else None
            answer[f"ndcg@{k}"] = None
            if complete and all(i in grades for i in ids):
                def dcg(values):
                    return sum((2 ** g - 1) / math.log2(rank + 2) for rank, g in enumerate(values))
                ideal = dcg(sorted((grades[i] for i in ids), reverse=True)[:k])
                answer[f"ndcg@{k}"] = dcg([grades[i] for i in top]) / ideal if ideal else None
        return answer

    return {"dataset_id": result["dataset_id"], "fold": fold,
            "scope": "ranking within the preassigned annotation fold, NOT full-corpus or web recall; null means insufficient labels",
            "methods": {name: metrics(ids) for name, ids in result["rankings"].items()},
            "by_venue": {key: {name: metrics([i for i in ids if venue(papers[i]) == key]) for name, ids in result["rankings"].items()}
                         for key in sorted({venue(papers[i]) for i in pool})}}
