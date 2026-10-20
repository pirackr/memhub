# memhub Layer 2 (search) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `memhub search` (FTS5/BM25 full-text search) and `memhub reindex` (index build/migration) on top of the layer-1 raw store.

**Architecture:** A v2 schema adds an FTS5 external-content table `memories_fts` over `memories`, kept in sync by triggers. `reindex` is the only migration/rebuild path (no implicit migration on open). A new `search.py` parses user queries into safe FTS5 MATCH strings and runs the ranked query; the CLI exposes `search` and `reindex` with the stable JSON envelope.

**Tech Stack:** Python 3.14, stdlib `sqlite3` (FTS5 available, verified: SQLite 3.54.0), `pytest`, `uv`.

**Spec:** `docs/superpowers/specs/2026-10-18-memhub-search-design.md` (approved)

## Global Constraints

- Python 3.14; stdlib only — no new runtime dependencies.
- CLI contract: JSON on stdout (`json.dumps(..., ensure_ascii=False)`, one line); errors are `memhub: <msg>` on stderr with exit 1; argparse usage errors exit 2; no traceback may escape.
- WAL checkpoint (TRUNCATE) on every command exit — file always safe to `cp`.
- Raw records are never re-interpreted: the FTS index is derived data; `reindex` can always rebuild it. Backfill preserves exact bytes, id, created_at, updated_at.
- Output envelope is always `{"hits": [...], "total": N, "hint": null|"..."}` — never a bare array.
- Hit fields (full names): `path`, `id`, `score`, `updated_at`, `snippet`, `match` (`"content"` | `"path"`).
- Defaults: top 10; `--limit` must be 1..50 (outside → usage error, exit 2 via argparse or MemhubError exit 1 — see Task 6); negative `--offset` → error.
- Snippet: FTS5 `snippet()` with empty markers, hard 200-char cap, match preserved; `snippet` is null for path-only (binary) hits.
- Stable order: score desc, then path asc, then id asc.
- Dates: UTC; date-only `--since` = start of UTC day; date-only `--until` = whole day (exclusive next-midnight internally); full timestamps require timezone (`Z` or `±hh:mm`), normalized to `YYYY-MM-DDTHH:MM:SSZ` (second precision); reversed range → error. Filter column: `updated_at`.
- Query syntax: bare words are exact tokens; prefix only via explicit trailing `*`; `"quoted phrases"` supported; invalid/unclosed syntax → clear memhub error; raw SQL/FTS5 error text never leaks.
- `search` on a v1 (unindexed) store fails with an actionable error naming `memhub reindex`; layer-1 commands stay fully usable on v1 stores.
- Tests: `pytest` (run via `uv run pytest`), tests in `tests/`.

## Review Focus

Failure modes no single task's happy-path tests exercise — each line gets a pinning test in the named task:

1. **FTS5 metacharacter input** (`" * ( ) : ^ + - "`, unclosed quote) must never leak a raw FTS5/SQL error or silently broaden the query — Task 4.
2. **Binary and invalid-UTF-8 rows**: path-only hits with `snippet: null`, `match: "path"`; invalid-UTF-8 `text/*` blobs stay byte-exact in `blob`, path-indexed, counted on stderr — Task 3 and Task 5.
3. **Index sync after every mutation kind** (put insert, put overwrite, mv rename, rm single, rm recursive) — FTS must match `memories` exactly; `reindex` on a v2 store must produce the same index as triggers — Task 2 and Task 3.
4. **v1 store behavior**: all layer-1 commands work unchanged on a v1 file; `search` errors actionably; `reindex` upgrades atomically and twice-concurrent reindex serializes — Task 3.
5. **Date boundary inputs**: date-only `--until` includes the whole UTC day; `±hh:mm` offsets normalize to UTC; reversed range and tz-less timestamp are errors — Task 6.

---

### Task 1: Layer-1 amendment — strict text routing in `put`

**Files:**
- Modify: `src/memhub/store.py` (`put`, its docstring)
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: existing `put(conn, path, data, content_type=None) -> Memory`.
- Produces: `put` routing rules used by Tasks 3/5: a row has non-NULL `content` iff its stored text is in the `content` column. Routing: explicit `text/*` → strict UTF-8 decode into `content`, `blob=NULL` (invalid UTF-8 → `MemhubError`); explicit non-text type → bytes in `blob`, `content=NULL`, no sniff override; no type given → existing `content_type_for` sniff (`text/plain` / `application/octet-stream`).

- [ ] **Step 1: Write failing tests** in `tests/test_store.py`:
  - `put(conn, "/a.md", b"# hi", content_type="text/markdown")` → `stat` shows `content_type == "text/markdown"`, and `SELECT content, blob FROM memories` shows `content == "# hi"`, `blob IS NULL`.
  - `put(conn, "/x.txt", b"\xff\xfe", content_type="text/plain")` still raises (existing behavior — keep).
  - `put(conn, "/x.bin", b"\xff\xfe", content_type="text/plain; charset=x")` — treat as `text/*` (prefix match on `"text/"`): raises for invalid UTF-8.
  - `put(conn, "/img.png", png_bytes, content_type="image/png")` where png_bytes is valid UTF-8 text bytes → stays in `blob` (no sniff override), `content IS NULL`.
  - Existing tests must keep passing unchanged.

- [ ] **Step 2: Run** `uv run pytest tests/test_store.py -v` — expect the new markdown test to FAIL (content lands in blob today).

- [ ] **Step 3: Implement** in `put`: replace `if ctype == "text/plain"` with `if ctype.startswith("text/")`; error message: `cannot store {p!r} as {ctype}: data is not valid UTF-8`. Update the `put` docstring to state the routing rules.

- [ ] **Step 4: Run** `uv run pytest tests/test_store.py -v` — all PASS.

- [ ] **Step 5: Commit** `git commit -am "feat: put stores any text/* content type in the content column"` (plus the test file).

---

### Task 2: Schema v2 — FTS table, triggers, version checks

**Files:**
- Modify: `src/memhub/store.py` (`_SCHEMA_VERSION`, `_SCHEMA`, `init_store`, `_open_store`)
- Test: `tests/test_store.py` (or new `tests/test_schema.py`)

**Interfaces:**
- Consumes: layer-1 `memories` table, `init_store(db_path)`, `open_store(db_path)`.
- Produces:
  - `_SCHEMA_VERSION = 2`; fresh `init_store` creates v2 (memories + `memories_fts` + triggers, `user_version = 2`).
  - `schema_version(conn) -> int` — reads `PRAGMA user_version`.
  - `_open_store` raises `MemhubError("store was created by a newer memhub version")` when `user_version > _SCHEMA_VERSION` (protects new code reading newer stores only).
  - `FTS_DDL` constant (the v2 DDL below) reused by Task 3's reindex.
  - Trigger sync semantics: after any put/mv/rm through the store API, `memories_fts` matches `memories` exactly (binary rows indexed with empty content via `coalesce(content, '')`).

- [ ] **Step 1: Write failing tests**:
  - fresh `init_store` → `schema_version == 2`; `sqlite_master` contains `memories_fts`, `memories_ai`, `memories_ad`, `memories_au`.
  - put a text memory → `SELECT path FROM memories_fts WHERE memories_fts MATCH 'widget'` returns it; put overwrite with new word → old word gone, new word found; `mv` → old path not found, new path found; `rm` → gone; `rm -r` subtree → all gone.
  - binary row (blob) → findable by a path word (`SELECT ... WHERE memories_fts MATCH 'pdf'` for `/docs/report.pdf`), content search term absent.
  - hand-build a v1 store (execute layer-1 `_SCHEMA` + `PRAGMA user_version = 1` on a fresh file), then `_open_store` works and `schema_version(conn) == 1` (no implicit migration).
  - store with `PRAGMA user_version = 3` → `open_store` raises "newer memhub version".

- [ ] **Step 2: Run** — expect FAIL (no fts table).

- [ ] **Step 3: Implement** in `store.py`:
  - `_SCHEMA_VERSION = 2`.
  - Keep `_SCHEMA` (v1 DDL) for the v1 fixture path; add `FTS_DDL`:

```sql
CREATE VIRTUAL TABLE memories_fts USING fts5(
  path, content, content='memories', content_rowid='rowid',
  tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER memories_ai AFTER INSERT ON memories BEGIN
  INSERT INTO memories_fts(rowid, path, content)
  VALUES (new.rowid, new.path, coalesce(new.content, ''));
END;
CREATE TRIGGER memories_ad AFTER DELETE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, path, content)
  VALUES ('delete', old.rowid, old.path, coalesce(old.content, ''));
END;
CREATE TRIGGER memories_au AFTER UPDATE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, path, content)
  VALUES ('delete', old.rowid, old.path, coalesce(old.content, ''));
  INSERT INTO memories_fts(rowid, path, content)
  VALUES (new.rowid, new.path, coalesce(new.content, ''));
END;
```

  - `init_store`: on a fresh store execute `_SCHEMA` + `FTS_DDL` and stamp 2; existing v1 stores are left untouched (no migration here); version > 2 rejection already exists — keep.
  - `_open_store`: after connect, check `PRAGMA user_version > _SCHEMA_VERSION` → raise. Note the check must not break opening v1 stores or the `mode=rw` URI path.
  - `schema_version(conn) -> int` public helper.

- [ ] **Step 4: Run** `uv run pytest tests/ -v` — all PASS (layer-1 CLI tests must not regress: triggers fire inside existing put/rm/mv transactions — `BEGIN IMMEDIATE` + FTS writes are fine in one txn).

- [ ] **Step 5: Commit** `"feat: schema v2 with FTS5 index and sync triggers"`.

---

### Task 3: `reindex` — migration, backfill, rebuild

**Files:**
- Modify: `src/memhub/store.py` (add `reindex`)
- Test: `tests/test_reindex.py` (new)

**Interfaces:**
- Consumes: `FTS_DDL`, `schema_version`, `_SCHEMA` (v1 fixture), `_begin_write`.
- Produces:

```python
class ReindexReport(NamedTuple):
    upgraded: bool      # v1 -> v2
    indexed: int        # rows in the rebuilt index
    backfilled: int     # text/* blob rows moved to content
    invalid_utf8: int   # text/* blob rows kept blob-only (path-indexed)

def reindex(conn: sqlite3.Connection) -> ReindexReport
```

  Semantics: `BEGIN IMMEDIATE`; re-check `user_version` after the lock (0 or >2 → `MemhubError("not a memhub store")` / newer-version error). Drop `memories_fts` + triggers if present; execute `FTS_DDL`; backfill (v1 upgrade path): for rows with `content_type LIKE 'text/%' AND content IS NULL`, strict-decode blob → set `content`, `blob=NULL`, keep id/created_at/updated_at/size; invalid UTF-8 → leave row untouched, count it. Then `INSERT INTO memories_fts(rowid, path, content) SELECT rowid, path, coalesce(content, '') FROM memories`; stamp `user_version = 2` when upgrading. Entire operation in one transaction — any failure rolls back completely.

- [ ] **Step 1: Write failing tests** in `tests/test_reindex.py`:
  - v1 fixture store (layer-1 `_SCHEMA`, version 1) with: a `text/markdown` row whose text sits in `blob` (pre-Task-1 data), a `text/plain` row with invalid UTF-8 in blob, a binary row. `reindex` → report `upgraded=True, backfilled=1, invalid_utf8=1, indexed=3`; store version 2; markdown row now searchable by a content word; its `size`, `created_at`, `updated_at`, `id` unchanged and bytes byte-exact via `read_content`; invalid row still byte-exact, findable by path only.
  - v2 store: mutate via API (put/mv/rm), then `reindex` → report `upgraded=False`; index identical to trigger-maintained index (compare `SELECT rowid, path, content FROM memories_fts ORDER BY rowid` before/after).
  - reindex twice on v2 → same result, no error.
  - concurrency: two connections call `reindex` on a v1 store; second gets `sqlite3.OperationalError` (database locked) or serializes to a no-op-upgrade — assert final state is a valid v2 store either way. (Keep deterministic: second attempt may use `timeout=0`.)
  - interrupted failure: monkeypatch to raise mid-reindex → store still v1, no `memories_fts` (full rollback).

- [ ] **Step 2: Run** — FAIL (`reindex` undefined).

- [ ] **Step 3: Implement** `reindex` per the interface above. Use `conn.execute("BEGIN IMMEDIATE")` (respect `in_transaction` like `_begin_write`). Backfill loop in Python (fetch candidate rows, decode per row, UPDATE). Report counts.

- [ ] **Step 4: Run** `uv run pytest tests/test_reindex.py tests/test_store.py -v` — PASS.

- [ ] **Step 5: Commit** `"feat: reindex command logic — v1 migration, blob backfill, index rebuild"`.

---

### Task 4: Query parser — `parse_query`

**Files:**
- Create: `src/memhub/search.py`
- Test: `tests/test_search.py` (new)

**Interfaces:**
- Consumes: nothing (pure function).
- Produces: `parse_query(text: str) -> str` — returns a safe FTS5 MATCH expression. Rules:
  - Tokenize `text` into bare words and `"quoted phrases"` (double quotes; an unclosed quote → `MemhubError('unbalanced quote in query')`).
  - Bare word: strip a single trailing `*` (prefix flag); the word is emitted as `"escaped-word"` + `*` if prefix, else `"escaped-word"`. Escaping = wrap in double quotes, doubling any `"` inside. Words that are empty after stripping → dropped.
  - Phrase: `"escaped-phrase"` (internal `"` doubled).
  - Join terms with ` AND `.
  - No terms after parsing → `MemhubError("search requires at least one term")`.
  - Never emits raw FTS5 operators; a query of only operators/punctuation (e.g. `"( ) :"`) yields no terms → the requires-at-least-one-term error, never a raw FTS5 syntax error downstream.

- [ ] **Step 1: Write failing tests** in `tests/test_search.py`:
  - `parse_query("hello world") == '"hello" AND "world"'`
  - `parse_query('cat*') == '"cat*"'`; `parse_query('pre-fix') == '"pre-fix"'` (hyphen safe inside quotes)
  - `parse_query('"exact phrase" tail') == '"exact phrase" AND "tail"'`
  - `parse_query('say "hi"') == '"say" AND "hi"'`
  - `parse_query('quote " inside')` raises unbalanced-quote error.
  - `parse_query('( ) ^ + : *')` raises requires-at-least-one-term (note: bare `*` alone is not a prefix term).
  - `parse_query('')` and `parse_query('   ')` raise requires-at-least-one-term.
  - `parse_query('""')` → phrase empty → dropped → requires-at-least-one-term.
  - Round-trip safety (Review Focus 1): for each nasty input in `['"; DROP', 'a"b', '"unclosed', 'AND OR', 'NEAR/a/b']`, `parse_query` either raises `MemhubError` or returns a string that `conn.execute("SELECT 1 WHERE memories_fts MATCH ?", (s,))` accepts on an empty v2 store without raising (build a throwaway v2 store in a fixture).

- [ ] **Step 2: Run** — FAIL (module missing).

- [ ] **Step 3: Implement** `src/memhub/search.py` with `parse_query` (a small hand-rolled scanner: iterate chars, toggle in-quote on `"`; whitespace splits outside quotes). Docstring states the rules.

- [ ] **Step 4: Run** — PASS.

- [ ] **Step 5: Commit** `"feat: safe FTS5 query parser for memhub search"`.

---

### Task 5: `search()` — ranked query, filters, snippets

**Files:**
- Modify: `src/memhub/search.py` (add `search`, `parse_bound`)
- Test: `tests/test_search.py`

**Interfaces:**
- Consumes: `parse_query` (Task 4), `schema_version` (Task 2), v2 store with triggers.
- Produces:

```python
class Hit(NamedTuple):
    path: str
    id: str
    score: float          # higher is better
    updated_at: str
    snippet: str | None   # None for path-only (binary) hits
    match: str            # "content" | "path"

class SearchResult(NamedTuple):
    hits: list[Hit]
    total: int

def parse_bound(value: str, kind: str) -> str   # kind in ("since","until")
def search(conn, query: str, since: str | None = None, until: str | None = None,
           path: str | None = None, limit: int = 10, offset: int = 0) -> SearchResult
```

  - `search` first checks `schema_version(conn) < 2` → `MemhubError("store is not indexed; run memhub reindex")`.
  - `parse_bound(value, kind)`: date-only `YYYY-MM-DD` → since: `YYYY-MM-DDT00:00:00Z`; until: next-day `T00:00:00Z` (exclusive bound stored as-is; `search` applies `updated_at < until_exclusive`). Full timestamp must carry `Z` or `±hh:mm` (else `MemhubError` naming the flag); normalize to UTC second precision `...Z`. Invalid format → `MemhubError`. `search` raises `MemhubError("--since must not be after --until")` when both given and since >= until-exclusive-normalized... (use strict `>` on normalized strings against the exclusive until bound: error iff `since > until_exclusive`).
  - `path` filter: component-aware prefix — keep rows where `m.path = :p OR m.path LIKE :p || '/%'` (escape `%`/`_`/`\` in `:p` via LIKE escape `\`). `path` is normalized with `store.normalize_path`.
  - SQL (single query; `total` via window `count(*) OVER ()`):

```sql
SELECT m.path, m.id, -bm25(memories_fts, 10.0, 1.0) AS score, m.updated_at,
       snippet(memories_fts, 1, '', '', ' ', 12) AS snip, count(*) OVER () AS total
FROM memories_fts
JOIN memories m ON m.rowid = memories_fts.rowid
WHERE memories_fts MATCH ?
  [AND m.updated_at >= ?] [AND m.updated_at < ?]
  [AND (m.path = ? OR m.path LIKE ? ESCAPE '\')]
ORDER BY bm25(memories_fts, 10.0, 1.0), m.path, m.id
LIMIT ? OFFSET ?
```

    Column weights: path 10.0, content 1.0 (path matches outrank content matches).
  - `match`/`snippet`: `snip` empty string → `match="path"`, `snippet=None`; else `match="content"` and snippet post-truncated to a hard 200-char cap that preserves the matched term (if `len > 200`, cut around the first case-insensitive occurrence of any query term so the term remains inside the window; else keep first 200).
  - `limit`/`offset` validated here: `limit` int 1..50 else `MemhubError("limit must be between 1 and 50")`; `offset >= 0` else `MemhubError("offset must not be negative")`.

- [ ] **Step 1: Write failing tests** in `tests/test_search.py`:
  - Ranking: doc with "widget" in content vs doc with "widget" in path (`/widget/notes.txt` content "gadget") → path hit scores higher (Review Focus: weights).
  - AND semantics: query "alpha beta" matches only doc containing both.
  - Prefix: `cat*` matches "catalog" content; `cat` does not match "catalog" (exact token default).
  - Phrase: `"red fox"` does not match "red quick fox".
  - Binary row → `match == "path"`, `snippet is None`.
  - Snippet: long doc (300+ chars around match) → `len(snippet) <= 200` and matched term present.
  - `total` counts all matches, `hits` respects limit/offset; offset beyond total → empty hits, correct total.
  - Filters: since/until on updated_at (put then re-put to move updated_at); path prefix `/a` matches `/a/b.txt` not `/ab.txt`; combined filters.
  - v1 store → "run memhub reindex" error.
  - `parse_bound` cases: `"2026-10-18"` since → `"2026-10-18T00:00:00Z"`; until → `"2026-10-19T00:00:00Z"`; `"2026-10-18T10:30:00+02:00"` → `"2026-10-18T08:30:00Z"`; `"2026-10-18T10:30:00"` (no tz) → error; reversed range error.
  - limit 0 / 51 / offset -1 → errors.

- [ ] **Step 2: Run** `uv run pytest tests/test_search.py -v` — expect FAIL (`search` undefined).

- [ ] **Step 3: Implement** as specified. Wrap `sqlite3.OperationalError` from the MATCH execution into `MemhubError("invalid search query")` (belt for Review Focus 1 — parser should prevent it).

- [ ] **Step 4: Run** — PASS.

- [ ] **Step 5: Commit** `"feat: ranked FTS search with date/path filters and snippets"`.

---

### Task 6: CLI — `search` and `reindex` commands

**Files:**
- Modify: `src/memhub/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `search.search`, `search.parse_bound`, `store.reindex`, `store.schema_version`, existing `_print_json`, `config.resolve_store`, `open_store`.
- Produces: `memhub search <query...> [--since D] [--until D] [--path P] [--limit N] [--offset N]` and `memhub reindex [--name N]` (name flag only if existing commands carry store selection via config — they do not; `reindex` takes no name, it uses `config.resolve_store()` like every other command).

  - `search` args: query is `nargs="+"` joined with spaces (so agents can pass words without shell quoting games). `--limit` type=int default 10, `--offset` type=int default 0 (range validation lives in `search.search` → MemhubError exit 1).
  - Envelope: `{"hits": [ {path,id,score,updated_at,snippet,match}, ... ], "total": N, "hint": h}`.
  - `hint` rules: `total == 0` → `"no matches; top-level paths: /a, /b, ..."` (up to 10 top-level entries from `ls(conn, "/")`, bounded); `offset + len(hits) < total` → `"<total> matches; showing <offset+1>-<offset+len(hits)>; narrow with --since/--until/--path or paginate with --offset"`; else `None`.
  - Exit 0 in all success cases including zero hits.
  - `reindex`: opens store, prints progress line to stderr (`reindexing...`), calls `store.reindex`, prints report JSON `{"upgraded": b, "indexed": n, "backfilled": n, "invalid_utf8": n}`.
  - Parser: two new subparsers; help strings in the existing style.

- [ ] **Step 1: Write failing tests** in `tests/test_cli.py` (follow existing CLI test style — they call `main([...])` with a tmp store env):
  - end-to-end: init, cp two text files, search a shared word → envelope with 2 hits, `total == 2`, `hint is None`, hit keys exactly `{path,id,score,updated_at,snippet,match}`.
  - zero hits → `{"hits": [], "total": 0, "hint": "no matches; top-level paths: ..."}`, exit 0.
  - truncation: 12 matching docs, default limit → 10 hits, total 12, hint mentions "12 matches"; `--offset 10` → 2 hits, hint None.
  - `--limit 51` → exit 1, stderr mentions limit; `--limit 0` → exit 1; `--offset -1` → exit 1.
  - `--since`/`--until` end-to-end with date-only values (pin the whole-day-until rule: doc updated today found with `--until <today>`).
  - `--path` filter narrows results.
  - v1 store (fixture) → `memhub search foo` exit 1, stderr contains "reindex"; `memhub reindex` exit 0 → then search works.
  - `memhub reindex` on v2 → report JSON, exit 0.
  - unclosed quote query → exit 1, stderr "unbalanced quote", no traceback.

- [ ] **Step 2: Run** — FAIL (no subcommands).

- [ ] **Step 3: Implement** `_cmd_search`, `_cmd_reindex`, parser entries. Keep `_cmd_search` thin: resolve store, `open_store`, parse bounds via `search.parse_bound`, call `search.search`, build envelope + hints (zero-hit hint uses `store.ls(conn, "/")`).

- [ ] **Step 4: Run** `uv run pytest tests/ -v` — full suite PASS.

- [ ] **Step 5: Commit** `"feat: memhub search and memhub reindex CLI commands"`.

---

### Task 7: Search benchmark harness

**Files:**
- Create: `benchmarks/bench_search.py`
- Test: none (benchmark script; smoke-run only)

**Interfaces:**
- Consumes: built CLI (`uv run memhub ...`), same conventions as `benchmarks/bench.py` (`run()` helper style, `--iters`).
- Produces: a script that generates a representative corpus (≥2,000 synthetic text docs, ~500 MB total, plus 50 binary blobs), then measures and prints (stderr + JSON via `--out`): `reindex` build time on the full corpus, per-mutation index cost (put/mv/rm with triggers active), query latency for 20 varied queries (common word, rare word, phrase, prefix, with `--since` filter), and final `memories_fts` index size (`SELECT ... FROM fts5vocab` or `pgsz*pgcount` from `pragma_page_count` delta). No numeric promises — the spec requires the data be recorded, not thresholds.

- [ ] **Step 1: Write** `benchmarks/bench_search.py` per the interface above (reuse `bench.py`'s subprocess/timing helpers by import or small copy).
- [ ] **Step 2: Smoke-run** `uv run python benchmarks/bench_search.py --iters 1 --out /tmp/bench_search.json` — completes, JSON written.
- [ ] **Step 3: Record** the smoke numbers in a short "Benchmark results" section appended to the spec doc, then commit `"bench: search/reindex benchmark harness + first corpus run"`.

---

## Self-review notes

- Spec coverage: put-routing amendment (T1), schema v2/triggers/version checks (T2), reindex migration+backfill+rebuild+concurrency (T3), query syntax (T4), ranking/snippets/filters/envelope internals (T5), CLI contract incl. hints and errors (T6), required benchmarks (T7). Date rules split: `parse_bound` in T5, end-to-end pin in T6.
- Type consistency: `Hit`/`SearchResult`/`ReindexReport`/`parse_bound` names used identically in T3/T5/T6.
- Review Focus lines map to tests: 1→T4 round-trip + T5 wrap; 2→T3 invalid-UTF-8 + T5 binary hit; 3→T2 trigger-sync + T3 rebuild-equality; 4→T3 concurrency/rollback + T6 v1 e2e; 5→T6 date pins.
