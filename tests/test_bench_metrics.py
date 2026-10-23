"""Tests for bench.metrics — plain IR definitions, binary relevance."""

import math

from bench import metrics

RANKED = ["a", "b", "c", "d"]
TRUTH = frozenset({"c"})


def test_recall_at_k():
    assert metrics.recall_at_k(RANKED, TRUTH, 1) == 0.0
    assert metrics.recall_at_k(RANKED, TRUTH, 2) == 0.0
    assert metrics.recall_at_k(RANKED, TRUTH, 3) == 1.0
    assert metrics.recall_at_k(RANKED, TRUTH, 4) == 1.0


def test_precision_at_k():
    # plain IR: denominator is k, not min(k, |truth|)
    assert metrics.precision_at_k(RANKED, TRUTH, 1) == 0.0
    assert metrics.precision_at_k(RANKED, TRUTH, 3) == 1 / 3
    assert metrics.precision_at_k(RANKED, TRUTH, 4) == 0.25


def test_hit_at_1():
    assert metrics.hit_at_1(RANKED, TRUTH) == 0.0
    assert metrics.hit_at_1(["c", "a"], TRUTH) == 1.0


def test_mrr_scans_top_k():
    assert metrics.mrr(RANKED, TRUTH, 4) == 1 / 3
    assert metrics.mrr(RANKED, TRUTH, 2) == 0.0  # 'c' beyond depth


def test_ndcg_at_k_binary():
    # DCG = 1/log2(4) (c at rank 3), IDCG = 1/log2(2) = 1
    assert metrics.ndcg_at_k(RANKED, TRUTH, 4) == 1 / math.log2(4)
    assert metrics.ndcg_at_k(["c", "a", "b", "d"], TRUTH, 4) == 1.0
    assert metrics.ndcg_at_k(RANKED, TRUTH, 2) == 0.0


def test_empty_truth_empty_ranked_all_one():
    empty = frozenset()
    assert metrics.recall_at_k([], empty, 5) == 1.0
    assert metrics.precision_at_k([], empty, 5) == 1.0
    assert metrics.hit_at_1([], empty) == 1.0
    assert metrics.mrr([], empty, 5) == 1.0
    assert metrics.ndcg_at_k([], empty, 5) == 1.0


def test_empty_truth_nonempty_ranked_all_zero():
    empty = frozenset()
    assert metrics.recall_at_k(["a"], empty, 5) == 0.0
    assert metrics.precision_at_k(["a"], empty, 5) == 0.0
    assert metrics.hit_at_1(["a"], empty) == 0.0
    assert metrics.mrr(["a"], empty, 5) == 0.0
    assert metrics.ndcg_at_k(["a"], empty, 5) == 0.0


def test_score_key_set():
    scores = metrics.score(RANKED, TRUTH, (1, 5, 10))
    assert set(scores) == {
        "recall@1", "recall@5", "recall@10",
        "precision@1", "precision@5", "precision@10",
        "hit@1", "mrr", "ndcg@10",
    }


def test_score_values():
    scores = metrics.score(RANKED, TRUTH, (1, 3))
    assert scores["recall@1"] == 0.0
    assert scores["recall@3"] == 1.0
    assert scores["precision@1"] == 0.0
    assert scores["precision@3"] == 1 / 3
    assert scores["hit@1"] == 0.0
    assert scores["mrr"] == 1 / 3  # depth = max(ks) = 3
    assert scores["ndcg@3"] == 1 / math.log2(4)


def test_score_empty_truth_convention():
    scores = metrics.score([], frozenset(), (1, 5))
    assert all(v == 1.0 for v in scores.values())
    scores = metrics.score(["a"], frozenset(), (1, 5))
    assert all(v == 0.0 for v in scores.values())
