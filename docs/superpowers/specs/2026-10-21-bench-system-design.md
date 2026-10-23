# memhub benchmark system — design

Date: 2026-10-21. Branch: `bench-system`. Status: draft for approval.

## Intent

Turn the ad-hoc benchmark scripts (currently in the external
`memhub-bench` workspace) into a proper, in-repo benchmark system so
that retrieval quality can be measured and compared as memhub grows
(vector search, grep verb, ranking changes, new query modes). Two axes
must be pluggable:

1. **Methods** — any retrieval approach that can ingest a corpus and
   answer ranked queries. Compare memhub variants (AND / keywords / OR)
   today; memhub-vector, grep, or third-party systems later.
2. **Datasets** — public memory benchmarks (LongMemEval oracle/S/M,
   LoCoMo) and our own custom benchmarks in a simple documented format.

The 2026-10-21 findings motivate this: memhub's BM25 ranking is
competitive (Recall@10 0.850 on LongMemEval-M vs 0.783 for the paper's
embedding retriever) but only under OR semantics; the system must make
exactly this kind of method-vs-method comparison routine.

## Goals

- One command runs a method × dataset matrix and prints a comparison
  table; machine-readable JSON results are saved with metadata (date,
  memhub version, dataset/method names, params).
- Adding a method = one small adapter class. Adding a dataset = one
  adapter or one data file in the custom format.
- Custom benchmark format simple enough to write by hand or generate.
- Deterministic: fixed seeds, no network at run time (dataset downloads
  are a separate, explicit step).
- Retrieval-only metrics: recall@k, precision@k, hit@1, MRR, nDCG@k,
  computed by tested code.

## Non-goals

- End-to-end QA evaluation (LLM answers + judge). Retrieval only.
- CI integration or benchmark history dashboards (later, maybe).
- Bundling dataset files in git (they are large; gitignored `data/`).
- Reproducing other systems (Zep/Mem0) — the method interface allows
  it, but no adapters ship for them now.

## Anti-goals

- Numbers that silently change meaning (metric bugs, changed defaults)
  — metrics are unit-tested and results carry version metadata.
- A system that only works for the two datasets it was born with.

## Constraints

- Python 3.14, stdlib only for the system itself (dataset loaders parse
  local JSON; no downloads inside the runner).
- Lives in the memhub repo on branch `bench-system`, under
  `benchmarks/` as a proper package (`python -m bench ...` from repo
  root or `uv run python -m bench ...`).
- Methods talk to memhub through its Python API (`memhub.store`,
  `memhub.search`) — no subprocess-per-document. The OR-mode method may
  use raw FTS5 SQL through the store connection, as the external bench
  does today.
- Existing external workspace `~/.../memhub-bench` is left untouched;
  this system supersedes it once merged.

## The parts (decided)

### Layout

```
benchmarks/
  bench/
    __init__.py
    cli.py            # argparse entry: python -m bench
    core.py           # Doc, Query, Judgments, ScoredResult dataclasses
    metrics.py        # recall@k, precision@k, hit@1, mrr, ndcg@k
    methods.py        # Method ABC + registry; memhub_and, memhub_kw,
                      # memhub_or implementations
    datasets.py       # Dataset ABC + registry; longmemeval (oracle/s/m),
                      # locomo, custom-jsonl
    custom.py         # loader for the custom benchmark format
    runner.py         # method x dataset matrix, per-item scoring,
                      # report rendering, JSON output
    download.py       # explicit dataset downloader (HF/LoCoMo URLs)
  README.md           # how to run, how to add a method/dataset
  data/               # gitignored: downloaded + custom datasets
  results/            # gitignored: JSON result files
```

`tests/test_bench_*.py` in the normal test tree cover metrics, the
custom-format loader, each memhub method, and a tiny end-to-end runner
pass on a fixture dataset (no network, <5 s).

### Core interfaces

```python
class Method:
    name: str
    def ingest(self, docs: list[Doc]) -> None: ...   # bulk
    def search(self, query: str, k: int) -> list[str]: ...  # doc ids

class Dataset:
    name: str
    def items(self) -> Iterator[Item]: ...
    # Item: id, group (question type), query text, truth: set[doc_id],
    #       corpus: list[Doc] (per-item haystack; shared-store datasets
    #       declare `shared=True` so the corpus ingests once)
```

`Doc` = (id, text, path=None, date=None). Memhub methods map id→path
(`/docs/<id>.md` by default). Truth is doc-id sets; ties and unjudged
docs follow standard TREC-style conventions (unjudged = irrelevant).

### Metrics (tested)

recall@k, precision@k (denominator min(k,|truth|) for recall-normalized
reporting is NOT used — plain IR definitions, documented), hit@1, MRR
computed over the full returned depth = max(k) (so MRR@10 with default
k), nDCG@k binary relevance. Defaults k = 1, 5, 10. (The external
prototype's MRR used top-50 depth; the new system's definition is
authoritative going forward and stated in every result file.)

### Methods shipped

- `memhub-and` — question through `memhub.search.parse_query` (AND).
- `memhub-kw` — stopword-stripped content words, ANDed.
- `memhub-or` — OR over content words via raw FTS5 SQL (reference).
All three: temp store per dataset corpus (or per item for
non-shared), `store.reindex`, then search. Stopword list moves into
`methods.py` (same list as the external bench).

### Datasets shipped

- `lme-oracle`, `lme-s`, `lme-m` — LongMemEval adapter (session-level
  truth), same rendering as the external bench (Date header + turns).
  **Revision pinned:** `download.py` fetches the ORIGINAL
  `xiaowu0162/longmemeval` files (the deprecated-but-still-served ones
  our reference numbers came from), NOT `longmemeval-cleaned` — the
  cleaned split removes noisy sessions and would silently change every
  number. URLs carry explicit revisions, and every result file records
  the SHA256 of each dataset file used, so a swapped revision can never
  silently alter comparability.
- `locomo` — turn-evidence → session truth, per-conversation shared
  store, skip empty-truth (adversarial) items, report by category.
- `custom` — the format below.

### Custom benchmark format (for our own benchmarks)

`data/<name>/corpus.jsonl`: one `{"id": "...", "text": "...",
"path": "optional", "date": "optional"}` per line.
`data/<name>/queries.jsonl`: one `{"id": "...", "group": "optional",
"query": "...", "truth": ["docid", ...]}` per line.
That is the whole contract. A generator script for synthetic
ground-truth corpora (port of `bench_accuracy.py`'s corpus builder)
ships as `bench/gen_synthetic.py --out data/<name>/` so custom
benchmarks can be materialized on demand. All six prototype query
kinds (word, prefix, phrase, path-only, multi-term, no-match) express
as truth sets (no-match = empty truth) and the generator emits them
with `group` set to the kind. Method-specific assertions the prototype
also made (`match=="path"` classification, snippet contains term) are
NOT part of the benchmark contract — they stay in memhub's pytest
suite where they already live.

### Runner & output

`uv run python -m bench --datasets lme-s --methods memhub-and,memhub-or
--limit 50 --k 1,5,10 --out results/<auto-name>.json`
Prints per-dataset tables grouped by item group (question type), one
table block per method, plus an ALL row; writes JSON with
`{meta: {date, memhub_version, k, limit, params}, results: [...]}`.
`--limit` bounds items (first N, deterministic order). On shared-store
datasets (locomo) `--limit` counts QA items, and a shared store is
ingested lazily on its first item's use, so limiting never ingests a
store the run doesn't query. Exit non-zero
on adapter errors; a method erroring on one item records the error and
continues.

## Left to the builder

- Exact class/function signatures, registry mechanics, and report
  formatting details.
- Whether shared-store datasets cache the ingested store across
  methods (they may; must be correct if so).
- Internal split of `datasets.py` if it grows.
