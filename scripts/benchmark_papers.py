"""Collect once, prepare evidence, compare offline, and score human labels."""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import os
import sqlite3
from importlib.resources import files
from pathlib import Path

import paper_scout
from state_store import StateStore
from ccf_paper_scout import benchmark as bench, discovery, proceedings, semantic


ROOT = Path(__file__).resolve().parents[1]


def source_hashes() -> dict:
    paths = [ROOT / "paper_scout.py", ROOT / "state_store.py", Path(__file__)]
    paths += sorted((ROOT / "src/ccf_paper_scout").glob("*.py"))
    paths += sorted((ROOT / "src/ccf_paper_scout/data").glob("*.json"))
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def offline_models() -> None:
    os.environ.setdefault("HF_HOME", str(ROOT / ".model-cache"))
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"


def collect(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=False)
    config = {"transport": "sparql", "page_size": 100, "max_pages_per_run": 12,
              "max_pages_per_partition_per_run": 3, "request_delay_seconds": 3,
              "max_attempts": 1, "timeout_seconds": 30, "overlap_pages": 1, "minimum_success_ratio": 1.0}
    venues = {v["dblp_key"]: v for v in bench.read_json(Path(str(files("ccf_paper_scout").joinpath("data/ccf_a_venues.json"))))["venues"]
              if v["dblp_key"] in ("aaai", "acl", "icml", "nips") and v["rank"] == "A"}
    store = StateStore(output / "acquisition.sqlite3")
    try:
        stats, failures = discovery.discover(store, venues, [2025], config, "ccf-paper-scout/0.2", paper_scout.fetch_dblp_page)
        papers = store.candidates(list(venues), [2025])
        _, qualification = proceedings.gate(papers, store)
        bench.write_json(output / "candidates.json", {"papers": papers, "discovery": stats,
                         "qualification": qualification, "failures": failures, "source_hashes": source_hashes()})
    finally:
        store.close()


def prepare(candidates: Path, output: Path, budget: int) -> None:
    if budget < 1:
        raise ValueError("budget must be positive")
    output.mkdir(parents=True, exist_ok=False)
    offline_models()
    source = bench.read_json(candidates)
    config = bench.config_for_experiment(bench.read_json(ROOT / "config.example.json"))
    papers = copy.deepcopy([p for p in source["papers"] if p["main_conference_status"] == "verified"])
    config["semantic"]["max_updated_papers_per_run"] = len(papers)
    store = StateStore(output / "vectors.sqlite3")
    try:
        paper_scout.apply_topic_priorities(paper_scout.rank_candidates([], papers, config["explicit_interests"]), config["topic_priority"])
        title_stats = semantic.rank(papers, [], config, store)
        if title_stats["deferred"]:
            raise ValueError("title scores incomplete")

        def checkpoint(current, stats):
            bench.write_json(output / "metadata-checkpoint.json", {"papers": current, "stats": stats})
            if stats["requests"] % 10 == 0:
                print(f"metadata requests={stats['requests']}/{budget}", flush=True)

        metadata_stats = bench.enrich(papers, budget, checkpoint=checkpoint)
        snapshot = {"format_version": 1, "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "config": config, "papers": sorted(papers, key=lambda p: p["id"]),
                    "acquisition_sha256": hashlib.sha256(candidates.read_bytes()).hexdigest(),
                    "source_hashes": source_hashes(), "discovery": source["discovery"],
                    "qualification": source["qualification"], "metadata": metadata_stats,
                    "title_semantic": title_stats, "metadata_request_budget": budget}
        snapshot["dataset_id"] = bench.digest(snapshot)
        bench.write_json(output / "snapshot.json", snapshot)
        print(f"Frozen {len(papers)} candidates; {sum(bool(p.get('abstract')) for p in papers)} abstracts.", flush=True)
    finally:
        store.close()


def compare(snapshot_path: Path, output: Path) -> None:
    snapshot = bench.read_json(snapshot_path)
    dataset_id = snapshot.pop("dataset_id")
    if bench.digest(snapshot) != dataset_id:
        raise ValueError("snapshot hash mismatch; do not edit frozen snapshots")
    output.mkdir(parents=True, exist_ok=False)
    offline_models()
    cache = output / "vectors.sqlite3"
    source_cache = snapshot_path.parent / "vectors.sqlite3"
    if source_cache.exists():
        source = sqlite3.connect(source_cache.resolve().as_uri() + "?mode=ro", uri=True)
        target = sqlite3.connect(cache)
        try:
            source.backup(target)
        finally:
            source.close()
            target.close()
    store = StateStore(cache)
    try:
        result = bench.compare(snapshot["papers"], snapshot["config"], store)
        replay = bench.compare(snapshot["papers"], snapshot["config"], store)
        if result["rankings"] != replay["rankings"] or result["baseline_selected_ids"] != replay["baseline_selected_ids"]:
            raise ValueError("offline replay changed the ordering")
        maximum_delta = max(abs(a["semantic_score"] - b["semantic_score"]) for a, b in zip(result["papers"], replay["papers"]))
        result.update(dataset_id=dataset_id, source_hashes=source_hashes(), metadata=snapshot["metadata"],
                      replay={"identical_order": True, "max_score_delta": maximum_delta, "semantic": replay["semantic"]})
        bench.write_json(output / "comparison.json", result)
        manifest = bench.sample_tasks(result)
        manifest["dataset_id"] = dataset_id
        bench.write_json(output / "annotation-manifest.json", manifest)
        bench.write_json(output / "labels-template.json", {"dataset_id": dataset_id, "labels": [
            {"id": p["id"], "grade": None, "role": "", "evidence": "", "reason": ""} for p in manifest["tasks"]]})
        from ccf_paper_scout.annotation_page import render
        (output / "annotate.html").write_text(render(manifest), encoding="utf-8")
        write_report(output / "comparison.md", result, manifest)
        print(f"Offline comparison complete; {len(manifest['tasks'])} blind annotation tasks. Replay identical.")
    finally:
        store.close()


def write_report(path: Path, result: dict, manifest: dict) -> None:
    lookup = {p["id"]: p for p in result["papers"]}
    lines = ["# 固定快照排序比较", "", f"数据集：`{result['dataset_id']}`", "",
             "这是单个固定兴趣画像的探索性实验。没有人工标签，暂不报告准确率或声称算法提升。",
             "三种原始排序共享有摘要且主会核验通过的候选池；词项基线另列现有过滤及配额后的结果。", "",
             "| 会议 | 核验通过 | 有摘要 |", "|---|---:|---:|"]
    for name, counts in result["metadata"]["by_venue"].items():
        lines.append(f"| {name} | {counts['candidates']} | {counts['abstracts']} |")
    for method, ids in result["rankings"].items():
        lines += ["", f"## {method} 原始排序 Top 10", ""]
        for rank, identifier in enumerate(ids[:10], 1):
            p = lookup[identifier]
            lines.append(f"{rank}. [{p['title']}]({p['url']}) — {p['venue']}；现有规则：`{p['selection_reason']}`")
    lines += ["", "## 现有词项规则输出（最多 10 篇）", ""]
    lines += [f"- {lookup[i]['title']}" for i in result["baseline_selected_ids"]]
    lines += ["", "## 下一步人工操作", "", "打开同目录 annotate.html；论文顺序已打乱，页面不显示模型分数或样本划分。",
              "先阅读标注准则，为每篇选择 0/1/2/待确认，复制摘要证据并写简短理由。定期导出 JSON 备份。",
              "标注页面可导入此前导出的 JSON 继续；提交文件须与本数据集匹配。",
              f"本批 {len(manifest['tasks'])} 篇，其中 calibration={list(manifest['folds'].values()).count('calibration')}，test={list(manifest['folds'].values()).count('test')}。",
              "先使用 calibration 评估和调整方案；方案冻结后再运行 test。不要查看 test 后反复调参。",
              "抽样来自前列并集、会议分层与随机补充；已知 DOI/同标题版本分在同一组，发现其他版本关联时须在评测前合并。",
              "评估仅针对预分配的标注池，不是全会议或全网召回。缺标签不算负例，样本不足时指标为空。", "",
              "## 回放检查", "", f"排序完全一致：{result['replay']['identical_order']}；最大分差：{result['replay']['max_score_delta']:.8g}。",
              f"第二轮新编码：{result['replay']['semantic']['encoded']}；向量缓存命中：{result['replay']['semantic']['cache_hits']}。"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    collect_parser = sub.add_parser("collect")
    collect_parser.add_argument("--output", type=Path, required=True)
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--candidates", type=Path, required=True)
    prepare_parser.add_argument("--output", type=Path, required=True)
    prepare_parser.add_argument("--budget", type=int, default=100)
    compare_parser = sub.add_parser("compare")
    compare_parser.add_argument("--snapshot", type=Path, required=True)
    compare_parser.add_argument("--output", type=Path, required=True)
    evaluate_parser = sub.add_parser("evaluate")
    evaluate_parser.add_argument("--comparison", type=Path, required=True)
    evaluate_parser.add_argument("--manifest", type=Path, required=True)
    evaluate_parser.add_argument("--labels", type=Path, required=True)
    evaluate_parser.add_argument("--fold", choices=("calibration", "test"), default="calibration")
    evaluate_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "collect":
        collect(args.output)
    elif args.command == "prepare":
        prepare(args.candidates, args.output, args.budget)
    elif args.command == "compare":
        compare(args.snapshot, args.output)
    else:
        if args.output.exists():
            raise FileExistsError("choose a new evaluation output path")
        bench.write_json(args.output, bench.evaluate(bench.read_json(args.comparison), bench.read_json(args.manifest),
                                                   bench.read_json(args.labels), args.fold))


if __name__ == "__main__":
    main()
