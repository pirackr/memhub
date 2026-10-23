# memhub benchmark system Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A proper in-repo benchmark system (`benchmarks/bench/`) that scores pluggable retrieval methods against public memory datasets and custom JSONL benchmarks, one command per method × dataset matrix.

**Architecture:** A `bench` package with two registries (methods, datasets) behind small ABCs; a runner that ingests per-store lazily, scores with pure tested metric functions, renders comparison tables, and writes JSON results carrying version + dataset-hash metadata. A pinned downloader and a synthetic-corpus generator feed it.

**Tech Stack:** Python 3.14 stdlib only; pytest; memhub's own `store`/`search` Python API.

**Spec:** `docs/superpowers/specs/2026-10-21-bench-system-design.md`

## Global Constraints

- Python 3.14, stdlib only for `bench/` (pytest is the only test dep).
- Package lives at `benchmarks/bench/`; importable as `bench` via pytest `pythonpath` config; CLI documented as `PYTHONPATH=benchmarks uv run python -m bench ...` from repo root.
- No network at benchmark run time; downloads are an explicit `bench download` subcommand.
- Dataset files and results are gitignored: `benchmarks/data/`, `benchmarks/results/`.
- Search depth k ≤ 50 (memhub `search` limit ceiling).
- Deterministic item order = dataset file order; `--limit` takes the first N items.
- Empty-truth convention (every metric): 1.0 if the method returned nothing, 0.0 otherwise.
- Stopword list and token regex port verbatim from the external prototype (`memhub-bench/bench_memory_datasets.py` lines 45–52).

## Review Focus

- **Empty-truth items** (nomatch queries, LoCoMo adversarial Qs): must not crash metrics or pollute aggregates — the 1.0-if-empty convention is pinned by tests.
- **Shared stores ingested once**: LoCoMo conversations carry ~200 QA items over one store; a store key must trigger exactly one ingest per method run (lazy, cached).
- **k > 50**: clear `BenchError` naming the ceiling, not a raw sqlite/limit traceback.
- **Method error on one item** (e.g. unparseable query text): recorded in results, run continues, item excluded from aggregates.
- **Dataset swap protection**: `bench download` refuses files whose SHA256 ≠ pinned value; every result JSON records the SHA256 of each dataset file used.

---

### Task 1: Package scaffold, core types, registries, metrics

**Files:**
- Create: `benchmarks/bench/__init__.py` (empty), `benchmarks/bench/core.py`, `benchmarks/bench/metrics.py`
- Modify: `pyproject.toml` (append `[tool.pytest.ini_options]` with `testpaths = ["tests"]` and `pythonpath = ["benchmarks"]`), `.gitignore` (append `benchmarks/data/` and `benchmarks/results/`)
- Test: `tests/test_bench_core.py`, `tests/test_bench_metrics.py`

**Interfaces:**
- Produces:
  - `core.BenchError(Exception)`
  - `core.Doc(id: str, text: str, path: str | None = None, date: str | None = None)` — frozen dataclass
  - `core.Item(id: str, group: str, query: str, truth: frozenset[str], store: str)` — frozen dataclass; `store` is the store key: items sharing a key share one ingested corpus
  - `core.Method` (ABC): `name: str`; `ingest(self, docs: Sequence[Doc]) -> None`; `search(self, query: str, k: int) -> list[str]` (ranked doc ids); `close(self) -> None` (default no-op)
  - `core.Dataset` (ABC): `name: str`; `items(self) -> Iterator[Item]`; `corpus(self, store_key: str) -> Iterator[Doc]`; `source_files(self) -> list[Path]` (default `[]`)
  - `core.register_method(name) -> decorator`, `core.register_dataset(name) -> decorator` (class-identity registration; factories are the classes)
  - `core.create_method(name: str) -> Method`, `core.create_dataset(name: str, data_dir: Path) -> Dataset`, `core.list_datasets(data_dir: Path) -> list[str]`
  - `metrics.recall_at_k(ranked: Sequence[str], truth: frozenset[str], k: int) -> float`, `precision_at_k(ranked, truth, k) -> float`, `hit_at_1(ranked, truth) -> float`, `mrr(ranked, truth, k: int) -> float`, `ndcg_at_k(ranked, truth, k: int) -> float` (binary relevance)
  - `metrics.score(ranked, truth, ks: tuple[int, ...]) -> dict[str, float]` — keys `recall@{k}` and `precision@{k}` for each k, `hit@1`, `mrr` (depth `max(ks)`), `ndcg@{max(ks)}`
- `create_dataset` dispatch: if `data_dir/<name>/corpus.jsonl` exists → `bench.custom.CustomDataset(data_dir, name)` (lazy import inside the function; Task 4 provides it); else registry lookup. Unknown name → `BenchError` listing available names (sorted).

- [ ] **Step 1: Write failing tests** — `tests/test_bench_metrics.py`: hand-computed fixtures, e.g. ranked `["a","b","c","d"]` truth `{"c"}` → recall@1 0.0, recall@3 1.0, precision@4 0.25, hit@1 0.0, mrr(k=4) 1/3, ndcg@4 1/log2(4)… compute exact values in the test; empty truth + empty ranked → every metric 1.0; empty truth + non-empty ranked → every metric 0.0; `score()` key set for `ks=(1,5,10)` is exactly `{recall@1,recall@5,recall@10,precision@1,precision@5,precision@10,hit@1,mrr,ndcg@10}`. `tests/test_bench_core.py`: registry round-trip (register a dummy Method/Dataset subclass, `create_method` returns instance, `create_method("nope")` raises `BenchError` mentioning available names); `list_datasets` finds a tmp dir containing `foo/corpus.jsonl` (create dummy file; monkeypatch or expect `BenchError` only when instantiating missing `queries.jsonl` — test just the registry path + unknown-name message).
- [ ] **Step 2: Run, verify failures** — `uv run pytest tests/test_bench_core.py tests/test_bench_metrics.py -q` → import errors.
- [ ] **Step 3: Implement** `core.py` and `metrics.py` per interfaces; plain IR definitions (recall denominator `|truth|`, precision denominator `k`); ndcg ideal discount over `min(k, |truth|)`; mrr scans `ranked[:k]`. Add pyproject/`.gitignore` edits.
- [ ] **Step 4: Run tests pass** — same command, all green; also `uv run pytest -q` full suite green.
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(bench): core types, registries, IR metrics"`

### Task 2: memhub retrieval methods

**Files:**
- Create: `benchmarks/bench/methods.py`
- Test: `tests/test_bench_methods.py`

**Interfaces:**
- Consumes: `core.Method`, `core.Doc`, `core.BenchError`; `memhub.store` (`init_store`, `open_store` or raw connect, `put`, `reindex`), `memhub.search` (`parse_query`, `search`).
- Produces: `methods.MemhubMethod` base (registered subclasses: `memhub-and` → `MemhubAnd`, `memhub-kw` → `MemhubKw`, `memhub-or` → `MemhubOr`); `methods.WORD_RE`, `methods.STOPWORDS` (ported verbatim).
- Base behavior: `__init__` makes a `tempfile.mkdtemp()` db via `store.init_store(Path)` and holds one `sqlite3.connect` connection; `ingest(docs)` puts each doc at `doc.path or f"/docs/{doc.id}"` as `"text/markdown"` (content `doc.text.encode()`), commits, runs `store.reindex(conn)` once, remembers `path → doc.id`; `search(query, k)` validates `1 <= k <= 50` else `BenchError("search depth k must be between 1 and 50")`, calls abstract `_search(query, k) -> list[str]` (paths), maps to ids (unknown path → drop); `close()` closes conn, `shutil.rmtree` temp dir (ignore errors).
- `MemhubAnd._search`: `parse_query(query)`; on `MemhubError` fall back to `" ".join(WORD_RE.findall(query.lower())) or "x"`; results = `[h.path for h in search.search(conn, q, limit=k).hits]`.
- `MemhubKw._search`: tokens = deduped `WORD_RE` tokens minus STOPWORDS; if empty, deduped all tokens, else `["x"]`; pass `" ".join(tokens)` through `search.search`.
- `MemhubOr._search`: same token rule; match string `" OR ".join(f'"{t}"' for t in tokens)`; raw SQL (port from prototype): `SELECT m.path, -bm25(memories_fts, 10.0, 1.0) s FROM memories_fts JOIN memories m ON m.rowid = memories_fts.rowid WHERE memories_fts MATCH ? ORDER BY s DESC LIMIT ?`.

- [ ] **Step 1: Write failing tests** — fixture ingests 5 docs (ids `d1..d5`) covering: doc containing both "alpha beta", one "alpha" only, one "beta" only, one with "what is the time" (pure stopwords), one "catalog item". Tests: `memhub-and` query "alpha beta" ranks the both-words doc first and excludes alpha-only; `memhub-or` returns union (≥ both docs); `memhub-kw` on "what is the time" falls back to raw tokens without error; `memhub-and` on unbalanced-quote query `"alpha` does not raise (fallback path); `memhub-or` matches "catalog" for query "catalog"; `search(q, 51)` raises `BenchError`; after `close()` the temp dir is gone.
- [ ] **Step 2: Verify fail** — `uv run pytest tests/test_bench_methods.py -q`.
- [ ] **Step 3: Implement** `methods.py` (all three classes registered via decorators; import memhub normally — `from memhub import store, search as msearch` — the package is an editable install under uv).
- [ ] **Step 4: Tests pass** — plus full suite.
- [ ] **Step 5: Commit** — `git commit -m "feat(bench): memhub AND/keyword/OR retrieval methods"`

### Task 3: Public dataset adapters (LongMemEval, LoCoMo)

**Files:**
- Create: `benchmarks/bench/datasets.py`
- Test: `tests/test_bench_datasets.py`

**Interfaces:**
- Consumes: `core.Dataset/Item/Doc/register_dataset`.
- Produces: `datasets.LmeDataset(data_dir: Path, split: str)` registered as `lme-oracle` / `lme-s` / `lme-m` (split from name suffix); file `data_dir / f"longmemeval_{split}.json"`. `datasets.LocomoDataset(data_dir)` registered `locomo`; file `data_dir / "locomo10.json"`.
- LME mapping (port prototype `lme_sessions`/`iter_longmemeval`): `Item(id=question_id, group=question_type, query=question, truth=frozenset(answer_session_ids), store=question_id)`; `corpus(store)` yields `Doc(id=sid, path=f"/sessions/{sid}.md", text=f"Date: {date}\n" + "\n".join(f"{t['role']}: {t['content']}" for t in turns))` zipping `haystack_session_ids`/`haystack_dates`/`haystack_sessions`; `source_files()` → `[file]`; missing file → `BenchError` naming the path and `bench download`.
- LoCoMo mapping (port `iter_locomo`): per conversation `conv["conversation"]`, session count = max `n` over `session_\d+` keys; session doc `id=f"{sample_id}/session{i}"`, `path=f"/conv/{sample_id}/session{i}.md"`, text `Date: {session_{i}_date_time}\n` + `"\n".join(f"{t['speaker']}: {t['text']}")`; QA items: evidence may be a `repr` string → `ast.literal_eval` guarded; truth = session doc ids from `D(\d+):` evidence prefixes; empty truth → skip item; `Item(id=f"{sample_id}:{qa_index}", group=f"cat{category}", query=question, store=sample_id)`; `corpus(store)` returns that conversation's session docs; `source_files()` → `[locomo file]`.

- [ ] **Step 1: Write failing tests** — build tiny fixture JSON files in `tmp_path` (2 questions × 2 sessions LME; 1 conversation × 2 sessions × 3 QA with one empty-evidence item skipped, one evidence given as string `"['D1:3']"`): assert item ids/groups/truth sets/store keys, corpus doc ids/paths/text rendering (Date header first line), `source_files()` listed, missing file raises `BenchError` containing "bench download".
- [ ] **Step 2: Verify fail**, **Step 3: Implement**, **Step 4: Tests pass**, **Step 5: Commit** — `git commit -m "feat(bench): LongMemEval and LoCoMo dataset adapters"`

### Task 4: Custom benchmark format + synthetic generator

**Files:**
- Create: `benchmarks/bench/custom.py`, `benchmarks/bench/gen_synthetic.py`
- Test: `tests/test_bench_custom.py`

**Interfaces:**
- Consumes: `core` (`Dataset`, `Item`, `Doc`, `BenchError`, `register_dataset` not needed — custom dirs are dispatched by `create_dataset`).
- Produces: `custom.CustomDataset(data_dir: Path, name: str)` reading `data_dir/name/corpus.jsonl` (`{"id","text","path"?,"date"?}` per line) and `queries.jsonl` (`{"id","query","truth":[...],"group"?}`); `store` key is always `"all"`; group defaults `"custom"`; malformed line (missing key / bad JSON) → `BenchError` with file + line number.
- `gen_synthetic.main(argv) -> int` with `--out DIR --docs N --seed S` (defaults 500/42); `gen_synthetic.generate(docs: int, seed: int) -> tuple[list[dict], list[dict]]` returns (corpus rows, query rows). Six groups, disjoint filler/probe word pools (port `make_words` from prototype `bench_accuracy.py`): `word` (probe embedded in k docs, query = probe), `prefix` (query = `f"{probe}*"`), `phrase` (query = `f'"{w1} {w2}"'`, truth = docs with the contiguous pair; distractor docs scatter w1/w2 non-contiguously), `path` (probe only in doc id/path `f"/docs/{probe}{n}.md"`, content filler-only), `multi` (query = two probes, truth = docs containing both), `nomatch` (unused probe, truth `[]`). Doc ids `doc{n:04d}`; all rows JSON-serializable.
- Note for README (Task 6): prefix queries exercise method differences — `memhub-and` honors `probe*` natively, `memhub-or` tokenizes it away. That is intended signal, not a bug.

- [ ] **Step 1: Write failing tests** — `generate(docs=40, seed=7)`: all six groups present; every non-nomatch query has non-empty truth; nomatch truth `[]`; path-group truth docs' text does NOT contain the probe word; phrase truth docs contain the contiguous pair. `CustomDataset` round-trip: write generated rows to `tmp_path/synth/{corpus,queries}.jsonl`, load via `core.create_dataset("synth", tmp_path)`, item counts match, corpus store `"all"`. Malformed `queries.jsonl` line → `BenchError` mentioning line number.
- [ ] **Step 2: Verify fail**, **Step 3: Implement**, **Step 4: Tests pass**, **Step 5: Commit** — `git commit -m "feat(bench): custom JSONL benchmark format and synthetic generator"`

### Task 5: Pinned dataset downloader

**Files:**
- Create: `benchmarks/bench/download.py`
- Test: `tests/test_bench_download.py`

**Interfaces:**
- Produces: `download.PINNED: dict[str, dict]` with keys `lme-oracle`, `lme-s`, `lme-m`, `locomo`; each `{"url", "target", "sha256"}`:
  - `lme-oracle` → `https://huggingface.co/datasets/xiaowu0162/longmemeval/resolve/2ec2a557f339b6c0369619b1ed5793734cc87533/longmemeval_oracle` → `longmemeval_oracle.json`, sha `821a2034d219ab45846873dd14c14f12cfe7776e73527a483f9dac095d38620c`
  - `lme-s` → same commit `/longmemeval_s` → `longmemeval_s.json`, sha `08d8dad4be43ee2049a22ff5674eb86725d0ce5ff434cde2627e5e8e7e117894`
  - `lme-m` → same commit `/longmemeval_m` → `longmemeval_m.json`, sha `fb5413e3b077c62927daab794836991a2fcfa61ceacab57dc679fb02daaff2d9`
  - `locomo` → `https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json` → `locomo10.json`, sha `79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4`
- `download.sha256_of(path: Path) -> str`; `download.fetch(name: str, data_dir: Path, force: bool = False, opener=urllib.request.urlopen) -> Path` — existing file with matching sha → return without network; mismatch (or force) → stream to `target.part`, verify, `os.replace`; sha mismatch after download → `BenchError` naming expected/got, `.part` removed; unknown name → `BenchError`. `opener` is the injection seam for tests (called as `opener(url)` returning a context-managed object with `.read(n)`).

- [ ] **Step 1: Write failing tests** — monkeypatched `opener` serving `b'{}'`: fetch writes target with content, second fetch does not call opener (cache hit), `force=True` re-downloads; wrong-sha fixture (content not matching PINNED) raises `BenchError` and leaves no `.part` or bad target; unknown name raises `BenchError`. Test `sha256_of` against a known tmp file.
- [ ] **Step 2: Verify fail**, **Step 3: Implement**, **Step 4: Tests pass**, **Step 5: Commit** — `git commit -m "feat(bench): pinned dataset downloader with sha256 verification"`

### Task 6: Runner, CLI, README

**Files:**
- Create: `benchmarks/bench/runner.py`, `benchmarks/bench/cli.py`, `benchmarks/bench/__main__.py`, `benchmarks/README.md`
- Test: `tests/test_bench_runner.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `runner.RunConfig(methods: list[str], datasets: list[str], data_dir: Path, limit: int | None, ks: tuple[int, ...] = (1, 5, 10), out: Path | None = None)`; `runner.run(cfg: RunConfig) -> dict` — loads each dataset once, each method fresh per (dataset, method), lazily ingests store keys (cached `set` per method run), searches at `k = max(ks)`, scores via `metrics.score`, aggregates mean per group + `ALL` row (item order preserved; groups sorted, `ALL` last), prints aligned tables per dataset×method, writes JSON when `out` set, returns the dict. Per-item exceptions → recorded `{"item": id, "error": str}`, excluded from aggregates, run continues.
- Result dict: `{"meta": {"date": <UTC iso>, "memhub_version": <importlib.metadata.version("memhub")>, "ks": [...], "limit": n|null, "datasets": {name: {filename: sha256}}, "methods": [...]}, "results": [{"dataset", "method", "n_items", "rows": [{"group", "n", <metric means...>}], "errors": [...]}]}`.
- `cli.main(argv: Sequence[str]) -> int`: subcommands `run` (`--datasets a,b` `--methods a,b` `--limit N` `--k 1,5,10` `--data-dir` default `benchmarks/data` resolved relative to the package, `--out` default `benchmarks/results/<datasets>-<methods>-<UTC %Y%m%dT%H%M%SZ>.json`, `--no-write`) and `download` (`name` positional, `--force`, `--data-dir`). Unknown method/dataset → `BenchError` message on stderr, exit 2. `__main__.py`: `raise SystemExit(main())`.
- README: quickstart (download → run → interpret), custom format spec, how to add a method/dataset (one class), metric definitions incl. empty-truth convention and MRR depth, pinned-revision rationale, note that `memhub-or` is the reference for what an OR query mode would give.

- [ ] **Step 1: Write failing tests** — register a toy `Method` (substring match over ingested docs) and toy `Dataset` (3 items, 2 store keys, one shared, groups `g1`/`g2`, one empty-truth item) in a fixture; `run()` with `limit=None`: JSON file exists with meta (date, memhub_version, dataset shas `{}` for toy, methods list) and rows for `g1`, `g2`, `ALL`; ingest count == number of distinct store keys (assert via toy counter — pins lazy shared-store behavior); `limit=2` truncates items; a toy method raising on one item produces an `errors` entry and correct remaining aggregates; `cli.main(["run", ...])` with `--no-write` returns 0 and prints a table containing `ALL`; unknown method returns 2.
- [ ] **Step 2: Verify fail**, **Step 3: Implement** runner + cli + `__main__` + README, **Step 4: Tests pass + full suite green**, **Step 5: Commit** — `git commit -m "feat(bench): matrix runner, CLI, README"`

### Task 7: Smoke validation on real data (no code gate)

**Files:** none (verification only; do not commit datasets or results).

- [ ] **Step 1:** `PYTHONPATH=benchmarks uv run python -m bench download lme-oracle` — file lands in `benchmarks/data/`, sha verified.
- [ ] **Step 2:** `PYTHONPATH=benchmarks uv run python -m bench run --datasets lme-oracle --methods memhub-and,memhub-or --limit 25` — table prints, results JSON written, OR recall ≫ AND recall (sanity vs reference: full-run OR recall@10 ≈ 0.99, AND ≈ 0.07).
- [ ] **Step 3:** `PYTHONPATH=benchmarks uv run python -m bench run --datasets <generated synth dir> --methods memhub-and --limit 50` via `gen_synthetic` output — word/prefix/phrase groups near 1.0.
- [ ] **Step 4:** If anything fails, fix forward in the owning task's file and amend with a follow-up commit.
