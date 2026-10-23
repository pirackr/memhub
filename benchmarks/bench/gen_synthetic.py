"""Deterministic synthetic corpus generator for the custom benchmark format.

Ported from memhub-bench/bench_accuracy.py (make_words, disjoint
filler/probe word pools, six query kinds) as pure functions: an explicit
seeded random.Random instead of the prototype's global rng hack, and no
CLI-subprocess ingestion — this only emits corpus/query rows in the
custom JSONL format (see bench.custom).

Six groups: word, prefix, phrase, path, multi, nomatch. Filler words are
never queried; probe words have known document sets by construction.
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

FILLER_POOL = 400          # distinct filler words (never queried)
FILLER_PER_DOC = 400       # filler words per document
PROBE_POOL = 200           # distinct probe words (queried, prefix-isolated)
PROBE_EMBED_MIN, PROBE_EMBED_MAX = 1, 8   # docs per word/prefix probe

N_WORD, N_PREFIX, N_PHRASE, N_PATH, N_MULTI, N_NOMATCH = 20, 10, 10, 10, 10, 5


def make_words(rng: random.Random, n: int, prefix: str) -> list[str]:
    """n distinct pronounceable synthetic words, prefixed for pool isolation."""
    words = set()
    while len(words) < n:
        w = "".join(rng.choice("bcdfghjklmnpqrstvwxyz") for _ in range(3))
        w += "".join(rng.choice("aeiou") for _ in range(3))
        words.add(f"{prefix}{w}")
    return sorted(words)


def generate(docs: int, seed: int) -> tuple[list[dict], list[dict]]:
    """Build a synthetic corpus and its exact-truth queries.

    Returns (corpus rows, query rows) in the custom JSONL row shapes:
    corpus ``{"id","text","path"}``, query ``{"id","query","truth","group"}``.
    Base doc ids are ``doc{n:04d}``; path-group docs extend the numbering.
    Deterministic for a given (docs, seed).
    """
    rng = random.Random(seed)
    filler = make_words(rng, FILLER_POOL, "")
    probes = make_words(rng, PROBE_POOL, "q")
    pi = 0

    def filler_text(n: int) -> str:
        return " ".join(rng.choice(filler) for _ in range(n))

    texts = [filler_text(FILLER_PER_DOC) for _ in range(docs)]

    def embed(indices: list[int], extra: str) -> None:
        for i in indices:
            texts[i] = texts[i] + " " + extra

    def pick(k: int) -> list[int]:
        return rng.sample(range(docs), min(k, docs))

    queries: list[dict] = []

    # word: probe embedded in k docs, query = probe
    for n in range(N_WORD):
        term = probes[pi]; pi += 1
        idx = pick(rng.randint(PROBE_EMBED_MIN, PROBE_EMBED_MAX))
        embed(idx, term)
        queries.append({"id": f"word-{n:02d}", "group": "word", "query": term,
                        "truth": [f"doc{i:04d}" for i in idx]})

    # prefix: same truth as word, query = probe + "*"
    for n in range(N_PREFIX):
        term = probes[pi]; pi += 1
        idx = pick(rng.randint(PROBE_EMBED_MIN, PROBE_EMBED_MAX))
        embed(idx, term)
        queries.append({"id": f"prefix-{n:02d}", "group": "prefix",
                        "query": f"{term}*",
                        "truth": [f"doc{i:04d}" for i in idx]})

    # phrase: contiguous pair in truth docs; distractors scatter the two
    # probe words non-contiguously (real filler words in the gaps —
    # punctuation would not occupy token positions and would leave the
    # pair genuinely adjacent).
    for n in range(N_PHRASE):
        a, b = probes[pi], probes[pi + 1]; pi += 2
        truth_idx = pick(rng.randint(2, 6))
        embed(truth_idx, f"{a} {b}")
        truth_set = set(truth_idx)
        for i in pick(5):
            if i not in truth_set:
                gap = " ".join(rng.choice(filler) for _ in range(5))
                embed([i], f"{a} {gap} {b}")
        queries.append({"id": f"phrase-{n:02d}", "group": "phrase",
                        "query": f'"{a} {b}"',
                        "truth": [f"doc{i:04d}" for i in truth_idx]})

    # path: probe ONLY in the doc path, content is filler-only. The probe
    # is a standalone path segment: unicode61 tokenizes "probe123" as one
    # token, so a probe adjacent to digits is unmatchable by a bare-probe
    # query and would make the truth unreachable for exact-token methods.
    path_docs: list[tuple[str, str]] = []
    for n in range(N_PATH):
        term = probes[pi]; pi += 1
        doc_num = docs + n
        path_docs.append((f"doc{doc_num:04d}", f"/docs/{term}/{doc_num}.md",
                          filler_text(FILLER_PER_DOC)))
        queries.append({"id": f"path-{n:02d}", "group": "path", "query": term,
                        "truth": [f"doc{doc_num:04d}"]})

    # multi: two probes; truth = docs containing BOTH (AND semantics).
    # Distractor docs receiving only-a or only-b are drawn disjoint from
    # the truth set and from each other, so no doc outside truth ever
    # contains both probes (truth must be exactly the docs containing both).
    for n in range(N_MULTI):
        a, b = probes[pi], probes[pi + 1]; pi += 2
        both = pick(rng.randint(1, 4))
        embed(both, a)
        embed(both, b)
        both_set = set(both)
        rest = [i for i in range(docs) if i not in both_set]
        distractors = rng.sample(rest, min(6, len(rest)))
        embed(distractors[:3], a)
        embed(distractors[3:], b)
        queries.append({"id": f"multi-{n:02d}", "group": "multi",
                        "query": f"{a} {b}",
                        "truth": [f"doc{i:04d}" for i in both]})

    # nomatch: probe never embedded anywhere, truth empty.
    for n in range(N_NOMATCH):
        term = probes[pi]; pi += 1
        queries.append({"id": f"nomatch-{n:02d}", "group": "nomatch",
                        "query": term, "truth": []})

    corpus = [{"id": f"doc{i:04d}", "text": texts[i],
               "path": f"/docs/doc{i:04d}.md"} for i in range(docs)]
    corpus.extend({"id": doc_id, "text": text, "path": path}
                  for doc_id, path, text in path_docs)
    return corpus, queries


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="bench gen-synthetic",
        description="Generate a synthetic benchmark in the custom JSONL "
                    "format (corpus.jsonl + queries.jsonl) under --out.")
    ap.add_argument("--out", required=True, type=Path,
                    help="output directory (created if missing)")
    ap.add_argument("--docs", type=int, default=500)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args(argv)

    corpus, queries = generate(args.docs, args.seed)
    args.out.mkdir(parents=True, exist_ok=True)
    for name, rows in (("corpus.jsonl", corpus), ("queries.jsonl", queries)):
        with (args.out / name).open("w") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
    print(f"wrote {len(corpus)} docs, {len(queries)} queries to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
