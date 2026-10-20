#!/usr/bin/env python3
"""Benchmark memhub search/reindex against a representative synthetic corpus.

Default corpus per the search spec: 2,000 synthetic text documents
(~250 KB each, ~500 MB total) plus 50 binary blobs. --scale F shrinks
the document/blob counts for smoke runs (0.01 -> 20 docs, 1 blob).

Every measurement is a real subprocess invocation (the honest unit an
agent pays), same convention as benchmarks/bench.py. Flow:

1. init a store, downgrade it to v1 (drop FTS table + triggers,
   user_version = 1) so ingestion runs without index maintenance;
2. ingest the corpus (total + per-doc wall time);
3. age half the rows' updated_at (so --since filters are meaningful);
4. time `memhub reindex` (v1 -> v2 migration + full index build);
5. time per-mutation index cost (put/mv/rm, sync triggers active);
6. time 20 varied queries (common/mid/rare word, phrase, prefix,
   --since filter), each --iters times, median reported;
7. measure final memories_fts size: fts5vocab term stats, plus the
   index bytes as a pragma_page_count delta (drop index + VACUUM on
   the throwaway store; fts5vocab exposes no page-size column).

No thresholds: the spec requires the data be recorded, not promised.

Usage: uv run python benchmarks/bench_search.py [--iters N] [--scale F]
                                                [--out results.json]
"""

import argparse
import json
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench import memhub_env, run, timed  # noqa: E402  shared helpers

MEMHUB = ["uv", "run", "memhub"]

# Representative corpus per the spec: >= 2,000 docs, ~500 MB text, 50 blobs.
DOC_COUNT = 2000
DOC_BYTES = 250 * 1024
BLOB_COUNT = 50
BLOB_BYTES = 1024 * 1024
MUTATION_BYTES = 100 * 1024
MASTER_BYTES = 8 * 1024 * 1024

# Rare tokens injected into exactly 3 docs each; MISSING_TOKEN is never written.
RARE_TOKENS = ("xenoflibble", "quorvathis", "mellifluvox", "brontibrate", "ziggurathic")
MISSING_TOKEN = "pliggoranth"

COMMON_WORDS = (
    "the of and to in a is that it for with on as by at this from or are was "
    "were be been which will can may not but also such using used use data "
    "memory system search document store index text file path record query "
    "result time node table row field value content size date update create "
    "read write line word token page block chunk sample corpus report note "
    "section entry list item version change summary draft final review"
).split()

_SYLLABLES = [
    a + b for a in "bcdfghjklmnpqrstvz" for b in ("a", "e", "i", "o", "u")
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def midwords(rnd: random.Random, n: int = 400) -> list[str]:
    """Deterministic invented words; a few are injected into a small share
    of the documents to act as mid-frequency query terms."""
    words = set()
    while len(words) < n:
        words.add("".join(rnd.choice(_SYLLABLES) for _ in range(rnd.randint(2, 4))))
    return sorted(words)


def build_master(rnd: random.Random) -> str:
    """One long word stream of common words; documents are slices of it."""
    parts = []
    total = 0
    while total < MASTER_BYTES:
        chunk = " ".join(rnd.choices(COMMON_WORDS, k=20000))
        parts.append(chunk)
        total += len(chunk) + 1
    return " ".join(parts)


def sample_phrases(rnd: random.Random, master: str, n: int) -> list[str]:
    """4-6 consecutive words from the master stream (so they exist in slices)."""
    out = []
    for _ in range(n):
        i = master.index(" ", rnd.randrange(len(master) // 2)) + 1
        j, words = i, 0
        while words < rnd.randint(4, 6):
            j = master.index(" ", j + 1)
            words += 1
        out.append(master[i:j])
    return out


def write_corpus(corpus_dir: Path, doc_count: int, blob_count: int,
                 rnd: random.Random, master: str, phrases: list[str],
                 mid_tokens: list[str]) -> int:
    """Write the synthetic corpus; returns total bytes. Mid-frequency
    tokens are injected into ~5% of documents, rare tokens into 3 each.
    Doc i lands at rowid i+1, and age_half() ages even rowids, so even
    doc indices stay 'recent' for --since queries."""
    inject = {t: set(rnd.sample(range(doc_count), max(1, round(doc_count * 0.05))))
              for t in mid_tokens}
    inject.update({t: set(rnd.sample(range(doc_count), min(3, doc_count)))
                   for t in RARE_TOKENS})
    phrase_docs = {p: set() for p in phrases}
    total = 0
    for i in range(doc_count):
        off = rnd.randrange(len(master) - DOC_BYTES)
        text = f"doc-{i:05d} synthetic benchmark document\n" + master[off:off + DOC_BYTES]
        extra = [t for t, docs in inject.items() if i in docs]
        if extra:
            text += "\n" + "".join(
                f"The {t} appears only in documents like this one.\n" for t in extra)
        for p in phrases:
            if p in text:
                phrase_docs[p].add(i)
        (corpus_dir / f"doc{i:05d}.txt").write_text(text)
        total += len(text)
    # Guarantee every phrase query matches; phrases[0] (used with --since)
    # must also match a recent (even-index) document.
    first = corpus_dir / "doc00000.txt"
    for p in phrases:
        if not phrase_docs[p] or (p is phrases[0]
                                  and not any(i % 2 == 0 for i in phrase_docs[p])):
            first.write_text(first.read_text() + f"\nappended phrase {p}\n")
    for i in range(blob_count):
        (corpus_dir / f"blob{i:03d}.bin").write_bytes(os.urandom(BLOB_BYTES))
        total += BLOB_BYTES
    return total


def ingest(paths: list[Path], env: dict) -> float:
    """cp every corpus file into /corpus/; returns total ms."""
    t0 = time.perf_counter()
    for p in paths:
        run(MEMHUB + ["cp", str(p), f"/corpus/{p.name}"], env=env)
    return (time.perf_counter() - t0) * 1000.0


def downgrade_to_v1(db: Path):
    """Simulate a pre-search (v1) store so reindex measures a full build."""
    con = sqlite3.connect(db)
    con.executescript(
        "DROP TRIGGER IF EXISTS memories_ai;"
        "DROP TRIGGER IF EXISTS memories_ad;"
        "DROP TRIGGER IF EXISTS memories_au;"
        "DROP TABLE IF EXISTS memories_fts;"
        "PRAGMA user_version = 1;"
    )
    con.commit()
    con.close()


def age_half(db: Path) -> int:
    """Set updated_at 30 days back on every other row (no triggers yet, so
    this is free); makes --since filters select roughly half the corpus."""
    old = (datetime.now(timezone.utc) - timedelta(days=30)) \
        .isoformat(timespec="seconds").replace("+00:00", "Z")
    con = sqlite3.connect(db)
    n = con.execute("UPDATE memories SET updated_at = ? WHERE rowid % 2 = 0",
                    (old,)).rowcount
    con.commit()
    con.close()
    return n


def run_json(cmd, env):
    """run() variant that captures a JSON stdout; returns (ms, parsed)."""
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE)
    dt = (time.perf_counter() - t0) * 1000.0
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd} failed: {proc.stderr.decode(errors='replace')[:200]}")
    return dt, json.loads(proc.stdout)


def bench_mutations(iters: int, env: dict, src: str) -> dict:
    """put/mv/rm with the sync triggers active on the fully indexed store."""
    put = [run(MEMHUB + ["cp", src, f"/mut/put{i}.txt"], env=env)
           for i in range(iters)]
    mv = []
    for i in range(iters):
        run(MEMHUB + ["cp", src, f"/mut/mvsrc{i}.txt"], env=env)
        mv.append(run(MEMHUB + ["mv", f"/mut/mvsrc{i}.txt", f"/mut/mvdst{i}.txt"],
                      env=env))
    rm = []
    for i in range(iters):
        run(MEMHUB + ["cp", src, f"/mut/rm{i}.txt"], env=env)
        rm.append(run(MEMHUB + ["rm", f"/mut/rm{i}.txt"], env=env))
    return {"put": timed(put), "mv": timed(mv), "rm": timed(rm)}


def build_queries(mids: list[str], phrases: list[str]) -> list[dict]:
    """20 varied queries: common/mid/rare words, a guaranteed miss, phrases,
    prefixes, and --since filters (half the corpus is 30 days old)."""
    today = datetime.now(timezone.utc)
    yesterday = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    tomorrow = (today + timedelta(days=1)).strftime("%Y-%m-%d")
    qs = [{"kind": "common-word", "args": [w]}
          for w in ("memory", "system", "document", "index")]
    qs += [{"kind": "mid-word", "args": [w]} for w in mids[:4]]
    qs += [{"kind": "rare-word", "args": [t]} for t in RARE_TOKENS[:2]]
    qs.append({"kind": "no-match", "args": [MISSING_TOKEN]})
    qs += [{"kind": "phrase", "args": [f'"{p}"']} for p in phrases[:3]]
    qs += [{"kind": "prefix", "args": [w[:3] + "*"]} for w in mids[4:7]]
    qs.append({"kind": "since", "args": ["memory", "--since", yesterday]})
    qs.append({"kind": "since", "args": [f'"{phrases[0]}"', "--since", yesterday]})
    qs.append({"kind": "since-empty", "args": ["memory", "--since", tomorrow]})
    assert len(qs) == 20, len(qs)
    return qs


def bench_queries(queries: list[dict], env: dict, iters: int) -> list[dict]:
    out = []
    for q in queries:
        runs, total = [], None
        for _ in range(iters):
            dt, res = run_json(MEMHUB + ["search", *q["args"]], env)
            runs.append(dt)
            total = res["total"]
        out.append({"kind": q["kind"], "args": q["args"],
                    "total_matches": total, **timed(runs)})
    return out


def index_size(db: Path) -> dict:
    """FTS index size as a page_count delta: count pages with the index,
    drop it, VACUUM, count again (the store is a throwaway temp dir).
    Term stats come from fts5vocab('row') while the index still exists."""
    con = sqlite3.connect(db)
    page = con.execute("PRAGMA page_size").fetchone()[0]
    with_pages = con.execute("PRAGMA page_count").fetchone()[0]
    db_with = db.stat().st_size
    con.execute("CREATE VIRTUAL TABLE main.mv USING fts5vocab(memories_fts, row)")
    terms, occurrences = con.execute(
        "SELECT count(*), coalesce(sum(cnt), 0) FROM main.mv").fetchone()
    rows = con.execute("SELECT count(*) FROM memories").fetchone()[0]
    con.executescript(
        "DROP TABLE main.mv;"
        "DROP TRIGGER IF EXISTS memories_ai;"
        "DROP TRIGGER IF EXISTS memories_ad;"
        "DROP TRIGGER IF EXISTS memories_au;"
        "DROP TABLE memories_fts;"
    )
    con.execute("VACUUM")
    without_pages = con.execute("PRAGMA page_count").fetchone()[0]
    con.close()
    return {"db_bytes_with_index": db_with, "page_size": page,
            "method": "page_count delta after DROP TABLE + VACUUM",
            "fts_bytes": max(with_pages - without_pages, 0) * page,
            "fts_terms": terms, "fts_occurrences": occurrences, "rows": rows}


def report(lines: list[str]) -> None:
    print("\n".join(lines), file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--iters", type=int, default=5,
                    help="repetitions per query/mutation (reindex runs once)")
    ap.add_argument("--scale", type=float, default=1.0,
                    help="shrink doc/blob counts; 1.0 = spec corpus (~500 MB)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    if args.scale <= 0:
        ap.error("--scale must be > 0")

    doc_count = max(1, round(DOC_COUNT * args.scale))
    blob_count = max(1, round(BLOB_COUNT * args.scale))
    rnd = random.Random(42)
    mids = midwords(rnd)

    tmp = Path(tempfile.mkdtemp(prefix="memhub-bench-search-"))
    lines: list[str] = []
    try:
        db = tmp / "store.sqlite"
        subprocess.run(
            MEMHUB + ["init", "bench", "--path", str(db)],
            env=memhub_env(tmp), check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        env = memhub_env(tmp)
        downgrade_to_v1(db)

        print(f"generating corpus: {doc_count} docs x {DOC_BYTES // 1024} KB "
              f"+ {blob_count} blobs ...", file=sys.stderr)
        corpus = tmp / "corpus"
        corpus.mkdir()
        mid_tokens = [w for w in mids if len(w) >= 3][:8]
        master = build_master(rnd)
        phrases = sample_phrases(rnd, master, 4)
        corpus_bytes = write_corpus(corpus, doc_count, blob_count, rnd,
                                    master, phrases, mid_tokens)
        # docs first so doc i lands at rowid i+1 (see write_corpus note)
        paths = sorted(corpus.glob("doc*.txt")) + sorted(corpus.glob("blob*.bin"))

        print("ingesting ...", file=sys.stderr)
        ingest_ms = ingest(paths, env)
        aged = age_half(db)

        print("reindexing ...", file=sys.stderr)
        reindex_ms, reindex_report = run_json(MEMHUB + ["reindex"], env)

        mut_src = tmp / "mutation-src.txt"
        mut_src.write_text(" ".join(rnd.choices(COMMON_WORDS, k=MUTATION_BYTES // 6)))
        mutations = bench_mutations(args.iters, env, str(mut_src))
        queries = bench_queries(build_queries(mid_tokens, phrases), env, args.iters)
        size = index_size(db)

        mb = 1024 * 1024
        lines.append(f"corpus      {doc_count} docs + {blob_count} blobs "
                     f"= {corpus_bytes / mb:.1f} MB (scale {args.scale})")
        lines.append(f"ingest      {ingest_ms:.0f} ms total, "
                     f"{ingest_ms / len(paths):.1f} ms/doc (v1 store, no index)")
        lines.append(f"reindex     {reindex_ms:.0f} ms v1->v2 build of "
                     f"{reindex_report['indexed']} rows "
                     f"(backfilled={reindex_report['backfilled']}, "
                     f"invalid_utf8={reindex_report['invalid_utf8']})")
        for op in ("put", "mv", "rm"):
            m = mutations[op]
            lines.append(f"mutation {op:<3} {MUTATION_BYTES // 1024} KB, triggers active: "
                         f"median {m['median_ms']:.1f} ms (n={m['runs']})")
        lines.append(f"{'kind':<12} {'query':<44} {'total':>7} {'median ms':>10}")
        for q in queries:
            lines.append(f"{q['kind']:<12} {' '.join(q['args']):<44} "
                         f"{q['total_matches']:>7} {q['median_ms']:>10.1f}")
        lines.append(f"index size  fts {size['fts_bytes'] / mb:.1f} MB "
                     f"({size['fts_terms']} terms, {size['fts_occurrences']} occurrences) "
                     f"/ db {size['db_bytes_with_index'] / mb:.1f} MB "
                     f"/ corpus {corpus_bytes / mb:.1f} MB, {size['rows']} rows")

        results = {
            "meta": {"timestamp": _now_iso(), "iters": args.iters,
                     "scale": args.scale, "docs": doc_count,
                     "doc_bytes": DOC_BYTES, "blobs": blob_count,
                     "blob_bytes": BLOB_BYTES, "corpus_bytes": corpus_bytes,
                     "aged_rows": aged},
            "ingest": {"total_ms": round(ingest_ms, 1),
                       "per_doc_ms": round(ingest_ms / len(paths), 1)},
            "reindex": {"ms": round(reindex_ms, 1), "report": reindex_report},
            "mutations": mutations,
            "queries": queries,
            "index_size": size,
        }
        if args.out:
            Path(args.out).write_text(json.dumps(results, indent=2))
        report(lines)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
