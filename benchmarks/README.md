# memhub benchmark system

Reproducible retrieval benchmarks for memhub: fixed datasets, pluggable
methods, one command per experiment. Python 3.14 stdlib only.

All commands run from the repo root:

```bash
export PYTHONPATH=benchmarks
```

## Quickstart

**1. Download a pinned dataset** (into `benchmarks/data/` by default):

```bash
uv run python -m bench download lme-oracle
```

Downloads are pinned to exact upstream revisions and sha256-verified (see
"Pinned revisions" below). Re-running is free: an existing file with the
expected sha256 is reused without network access.

**2. Run the matrix** (every dataset × method pair):

```bash
uv run python -m bench run --datasets lme-oracle --methods memhub-and,memhub-or
```

Useful flags:

- `--limit N` — only the first N items per dataset (cheap smoke runs)
- `--k 1,5,10` — metric cutoffs; search depth is always `max(ks)`
- `--data-dir DIR` — dataset directory (default `benchmarks/data`)
- `--out FILE` / `--no-write` — result JSON path (default
  `benchmarks/results/<datasets>-<methods>-<UTC stamp>.json`) or none

An aligned table prints per dataset × method, e.g.:

```
=== dataset=lme-oracle method=memhub-and n_items=25 errors=0 ===
group  n  recall@1  precision@1  ...  hit@1  mrr    ndcg@10
-----  --  --------  -----------  ...  -----  -----  -------
sqa    5  0.200     0.200        ...  0.200  0.267  0.244
ALL    25 0.072     0.072        ...  0.072  0.098  0.101
```

**3. Interpret.** The `ALL` row is the headline number per pair. Compare
methods on the same dataset; compare groups (`group` rows) to see *where*
a method wins or loses. Per-item failures are recorded in the JSON
`errors` list (item id + message) and excluded from aggregates — a run
with errors still completes. Unknown method/dataset names exit 2 with a
message listing what is available.

## Methods

| name | what it is |
|---|---|
| `memhub-and` | the query passed through memhub's own parser (all terms ANDed) — what memhub does today |
| `memhub-kw` | stopword-stripped content words, ANDed |
| `memhub-or` | OR over content words, raw FTS5 — **the reference point for what an OR query mode would give**; memhub's public API cannot express OR, so this bypasses it |

Large `memhub-or` minus `memhub-and` gaps mean OR semantics (any-term
matching) would recover most of the loss; small gaps mean the loss is
elsewhere (tokenization, ranking).

## Datasets

- `lme-oracle`, `lme-s`, `lme-m` — LongMemEval (oracle/small/main splits);
  one ingested store per question, truth = answer session ids
- `locomo` — LoCoMo conversations; one store per conversation, truth =
  evidence sessions (unanswerable items without retrieval truth are skipped)
- custom directories — any `data-dir/<name>/` with `corpus.jsonl` +
  `queries.jsonl` is auto-detected (see below)

### Custom format

`corpus.jsonl`, one JSON object per line: `{"id", "text", "path"?, "date"?}`
`queries.jsonl`: `{"id", "query", "truth", "group"?}` — `truth` is a list
of corpus doc ids; `group` defaults to `"custom"`. Every item shares one
store (`"all"`). Malformed lines fail with the file and line number.

A deterministic synthetic generator is available:

```bash
uv run python -m bench.gen_synthetic --out benchmarks/data/synth
uv run python -m bench run --datasets synth --methods memhub-and
```

## Metrics

Binary relevance; each item is scored at search depth `k = max(ks)`:

- `recall@k` — fraction of truth docs in the top k
- `precision@k` — fraction of the top k that are truth docs
- `hit@1` — 1.0 iff the top result is a truth doc
- `mrr` — reciprocal rank of the first truth doc, **computed at depth
  `max(ks)`** (the external prototype's top-50 MRR is history; MRR here is
  comparable only at the same `ks`)
- `ndcg@max(ks)` — nDCG with binary gains and ideal discount over
  `min(k, |truth|)`

**Empty-truth convention:** items whose truth set is empty score **1.0 on
every metric iff the method returned nothing, 0.0 otherwise** — a perfect
score means "correctly abstained", not "nothing to measure".

Aggregates are per-group means plus a final `ALL` row; the JSON file
records `meta` (UTC date, memhub version, `ks`, `limit`, per-dataset
source-file sha256s, methods) for provenance.

## Pinned revisions

`bench download` fetches from fixed commit URLs with mandatory sha256
verification, so a `meta` block plus the pinned `PINNED` table fully
reconstructs the inputs years later. Upstream repos move; pins do not.
To bump a pin, update `benchmarks/bench/download.py` (`url` + `sha256`)
and re-run — a mismatch fails loudly and leaves no partial file.

## Adding a method or dataset

One class each; the registry does the rest.

Method (`benchmarks/bench/methods.py`):

```python
@register_method("my-method")
class MyMethod(Method):
    name = "my-method"
    def ingest(self, docs): ...          # Sequence[Doc] -> None
    def search(self, query, k): ...      # -> up to k ranked doc ids
    def close(self): ...                 # optional cleanup
```

Dataset (`benchmarks/bench/datasets.py`):

```python
@register_dataset("my-data")
class MyDataset(Dataset):
    def __init__(self, data_dir, name): ...
    def items(self): ...                 # yields Item(id, group, query, truth, store)
    def corpus(self, store_key): ...     # yields Doc for one store
    def source_files(self): ...          # files to hash into meta
```

Items sharing a `store` key share one ingested corpus; the runner ingests
each store lazily (only when its first item is reached) exactly once per
method run.

## Method-difference notes

These are intended signal, not bugs — they are exactly what the benchmark
is built to expose:

- **Prefix queries** (e.g. `probe*`): `memhub-and` honors the wildcard
  natively through memhub's parser; `memhub-or` tokenizes it away into a
  plain term. Expect the two to diverge on prefix groups.
- **Path-group probes adjacent to digits** (e.g. `qxyz123.md`): whether a
  method's matching tokenizes `qxyz123` as containing `qxyz` is
  method-dependent; path-group scores differ across methods for this
  reason.
