"""Optional CPU embeddings; no ML dependency is imported by the base CLI."""
from __future__ import annotations

import hashlib
import json
import math
import re
import struct
from typing import Any

MODEL = "BAAI/bge-small-en-v1.5"
REVISION = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def normalized(values: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in values))
    if not norm or not math.isfinite(norm):
        raise ValueError("embedding must be finite and non-zero")
    return [v / norm for v in values]


def cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise ValueError("embedding dimensions do not match")
    return sum(a * b for a, b in zip(left, right))


class Encoder:
    def __init__(self, config: dict[str, Any], model: Any = None):
        self.config = config
        name = str(config.get("model", MODEL))
        revision = str(config.get("revision", REVISION))
        if name != MODEL:
            raise ValueError("first semantic release supports BAAI/bge-small-en-v1.5 only")
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("semantic.revision must be a pinned model commit")
        self.signature = fingerprint([name, revision, config.get("max_chunks_per_paper", 3), "title-chunks-mean-l2-v1", QUERY_PREFIX])
        self.batch = int(config.get("batch_size", 16))
        if self.batch < 1 or int(config.get("max_chunks_per_paper", 3)) < 1:
            raise ValueError("semantic batch and chunk limits must be positive")
        if config.get("device", "cpu") != "cpu":
            raise ValueError("this deployment uses CPU inference")
        if model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise RuntimeError("install ccf-paper-scout[semantic] to enable embeddings") from exc
            model = SentenceTransformer(name, revision=revision, device="cpu", trust_remote_code=False)
        self.model = model

    def encode_papers(self, papers: list[dict[str, Any]]) -> list[list[float]]:
        tokenizer = self.model.tokenizer
        limit = min(int(self.model.max_seq_length), 512)
        texts: list[str] = []
        spans = []
        for paper in papers:
            title = " ".join(str(paper.get("title", "")).split())
            abstract = " ".join(str(paper.get("abstract", "")).split())
            title_ids = tokenizer.encode(title, add_special_tokens=False)[:min(96, limit // 3)]
            abstract_ids = tokenizer.encode(abstract, add_special_tokens=False)
            available = limit - len(title_ids) - 8
            chunks = [abstract_ids[i:i + available] for i in range(0, len(abstract_ids), available)] or [[]]
            maximum = int(self.config.get("max_chunks_per_paper", 3))
            paper["semantic_truncated"] = len(chunks) > maximum
            start = len(texts)
            for chunk in chunks[:maximum]:
                texts.append(tokenizer.decode(title_ids, skip_special_tokens=True) + "\n" + tokenizer.decode(chunk, skip_special_tokens=True))
            spans.append((start, len(texts)))
        if not texts:
            return []
        vectors = self.model.encode(texts, batch_size=self.batch, normalize_embeddings=True, show_progress_bar=False)
        result = []
        for start, end in spans:
            result.append(normalized([sum(float(vectors[i][j]) for i in range(start, end)) / (end - start) for j in range(len(vectors[start]))]))
        return result

    def encode_query(self, text: str) -> list[float]:
        vector = self.model.encode([QUERY_PREFIX + text], normalize_embeddings=True, show_progress_bar=False)[0]
        return normalized([float(v) for v in vector])


def rank(papers: list[dict[str, Any]], interests: list[dict[str, Any]], config: dict[str, Any], store: Any,
         *, encoder: Encoder | None = None) -> dict[str, Any]:
    settings = config.get("semantic", {})
    encoder = encoder or Encoder(settings)
    topics = config.get("research_profile", {}).get("directions") or [
        {"name": topic, "description": topic} for topic in config.get("explicit_interests", [])
    ]
    if not topics:
        raise ValueError("semantic ranking requires research directions")
    references = [{key: item.get(key, "") for key in ("key", "id", "title", "abstract")}
                  for item in interests[:int(settings.get("max_reference_papers", 20))] if item.get("title")]
    reference_weight = float(settings.get("reference_weight", 0.15))
    if not 0 <= reference_weight <= 0.5:
        raise ValueError("semantic.reference_weight must be between 0 and 0.5")
    profile_hash = fingerprint([topics, references, reference_weight, config.get("topic_priority", {}), encoder.signature])
    reference_vectors = []
    for item in references:
        hashed = fingerprint([item["title"], item["abstract"]])
        key = "reference:" + hashed
        cached = store.load_vector(key, hashed, encoder.signature)
        vector = list(struct.unpack(f"<{len(cached) // 4}f", cached)) if cached else encoder.encode_papers([item])[0]
        if not cached:
            store.save_vector(key, hashed, encoder.signature, len(vector), struct.pack(f"<{len(vector)}f", *vector))
        reference_vectors.append((item, vector))
    profiles = []
    for topic in topics:
        text = topic.get("description", topic["name"])
        key = "profile:" + fingerprint(text)
        cached = store.load_vector(key, fingerprint(text), encoder.signature)
        vector = list(struct.unpack(f"<{len(cached) // 4}f", cached)) if cached else encoder.encode_query(text)
        if not cached:
            store.save_vector(key, fingerprint(text), encoder.signature, len(vector), struct.pack(f"<{len(vector)}f", *vector))
        profiles.append((topic["name"], vector))
    vectors: dict[str, list[float]] = {}
    pending = []
    hits = 0
    for paper in papers:
        hashed = fingerprint([paper.get("title", ""), paper.get("abstract", "")])
        cached = store.load_vector("paper:" + paper["id"], hashed, encoder.signature)
        if cached:
            vectors[paper["id"]] = list(struct.unpack(f"<{len(cached) // 4}f", cached))
            hits += 1
        else:
            pending.append(paper)
    maximum = max(0, int(settings.get("max_updated_papers_per_run", 500)))
    to_encode = pending[:maximum]
    encoded = encoder.encode_papers(to_encode)
    for paper, vector in zip(to_encode, encoded):
        vectors[paper["id"]] = vector
        store.save_vector("paper:" + paper["id"], fingerprint([paper.get("title", ""), paper.get("abstract", "")]), encoder.signature,
                          len(vector), struct.pack(f"<{len(vector)}f", *vector))
    for paper in papers:
        vector = vectors.get(paper["id"])
        if vector is None:
            paper["semantic_deferred"] = True
            continue
        paper.pop("semantic_deferred", None)
        reference_scores = sorted([(cosine(vector, ref), item["title"]) for item, ref in reference_vectors], reverse=True)
        auxiliary = sum(score for score, _ in reference_scores[:3]) / min(3, len(reference_scores)) if reference_scores else 0.0
        weight = reference_weight if reference_scores else 0.0
        scores = {name: (1 - weight) * cosine(vector, profile) + weight * auxiliary for name, profile in profiles}
        paper["semantic_references"] = [title for _, title in reference_scores[:3]]
        paper["direction_scores"] = scores
        paper["semantic_score"] = max(scores.values())
        paper["semantic_direction"] = max(scores, key=scores.get)
    return {"cache_hits": hits, "encoded": len(encoded), "deferred": len(pending) - len(to_encode),
            "profile_hash": profile_hash, "model_signature": encoder.signature}


def calibrated_thresholds(config: dict[str, Any], stats: dict[str, Any], *, preview: bool) -> dict[str, float] | None:
    calibration = config.get("selection", {}).get("calibration")
    if not calibration:
        if preview:
            return None
        raise ValueError("semantic production requires an approved calibration; run evaluate/preview first")
    if (calibration.get("model_signature") != stats["model_signature"] or
            calibration.get("profile_hash") != stats["profile_hash"] or not calibration.get("approved")):
        raise ValueError("calibration does not match the current model and research profile")
    thresholds = calibration.get("thresholds", {})
    if set(thresholds) != {"abstract", "title_only"} or any(not isinstance(v, (int, float)) or not -1 <= v <= 1 for v in thresholds.values()):
        raise ValueError("calibration must specify abstract and title_only cosine thresholds")
    return thresholds
