"""IR metrics with binary relevance.

Plain IR definitions: recall denominator is |truth|, precision denominator
is k. Empty-truth convention: every metric is 1.0 if the ranked list is
empty, else 0.0.
"""

from __future__ import annotations

import math
from typing import Sequence


def _empty_truth_value(ranked: Sequence[str]) -> float:
    return 1.0 if not ranked else 0.0


def recall_at_k(ranked: Sequence[str], truth: frozenset[str], k: int) -> float:
    if not truth:
        return _empty_truth_value(ranked)
    hits = len(set(ranked[:k]) & truth)
    return hits / len(truth)


def precision_at_k(ranked: Sequence[str], truth: frozenset[str], k: int) -> float:
    if not truth:
        return _empty_truth_value(ranked)
    hits = len(set(ranked[:k]) & truth)
    return hits / k


def hit_at_1(ranked: Sequence[str], truth: frozenset[str]) -> float:
    if not truth:
        return _empty_truth_value(ranked)
    return 1.0 if ranked and ranked[0] in truth else 0.0


def mrr(ranked: Sequence[str], truth: frozenset[str], k: int) -> float:
    """Reciprocal rank of the first relevant doc within ranked[:k]."""
    if not truth:
        return _empty_truth_value(ranked)
    for i, doc_id in enumerate(ranked[:k]):
        if doc_id in truth:
            return 1.0 / (i + 1)
    return 0.0


def ndcg_at_k(ranked: Sequence[str], truth: frozenset[str], k: int) -> float:
    """nDCG@k, binary relevance; ideal discount over min(k, |truth|)."""
    if not truth:
        return _empty_truth_value(ranked)
    dcg = 0.0
    for i, doc_id in enumerate(ranked[:k]):
        if doc_id in truth:
            dcg += 1.0 / math.log2(i + 2)
    idcg = sum(1.0 / math.log2(i + 2) for i in range(min(k, len(truth))))
    return dcg / idcg


def score(ranked: Sequence[str], truth: frozenset[str], ks: tuple[int, ...]) -> dict[str, float]:
    """Score one item: recall@k/precision@k for each k, hit@1, mrr and
    ndcg at depth max(ks)."""
    depth = max(ks)
    out: dict[str, float] = {}
    for k in ks:
        out[f"recall@{k}"] = recall_at_k(ranked, truth, k)
        out[f"precision@{k}"] = precision_at_k(ranked, truth, k)
    out["hit@1"] = hit_at_1(ranked, truth)
    out["mrr"] = mrr(ranked, truth, depth)
    out[f"ndcg@{depth}"] = ndcg_at_k(ranked, truth, depth)
    return out
