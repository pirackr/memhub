# memhub — Layer 2 (search) design

Date: 2026-10-18
Status: approved by partner in brainstorm conversation
Builds on: docs/superpowers/specs/2026-10-18-memhub-design.md (layer 1)

## Intent

Add full-text search to memhub so an agent can find memories by content.
This is the first "higher layer" built on the layer-1 raw store, using the
extension seam layer 1 left (schema migration via `user_version`).

Research grounding (summarized from a two-report survey; full findings in
the brainstorm conversation): lexical BM25 is the strongest cheap baseline
for agent memory retrieval; agents write keyword-shaped queries and search
iteratively; small inline results beat large dumps; delivery format
matters as much as the retriever. FTS5 search first, grep as a possible
cheap verb later.

## Expected scale

Thousands of memories, gigabytes of content, mostly text docs, occasional
PDFs. FTS5 handles this with headroom; performance is not a design driver.

## Goals (v1)

- `memhub search <query> [flags]` returning ranked hits as JSON.
- Indexed: the text content of memories, plus the path as a separate
  column weighted above content (a query matching the path scores
  higher). The path is indexed for EVERY row, including binaries — so
  PDFs are findable by path/name (their snippet is null). Content
  extraction for binaries is a deferred layer.
- Layer-1 amendment (builder ruling): `put` treats any content type
  starting with `text/` (or sniffed valid UTF-8) as text and fills the
  `content` column — otherwise docs tagged `text/markdown` would land in
  the blob column and be invisible to search. Raw storage stays
  verbatim; only the text/blob routing widens.
- Ranking: SQLite FTS5 Okapi-BM25 (`ORDER BY rank`). No recency blending
  in v1; dates ride along on every hit.
- Query handling: user input is sanitized into a safe FTS5 query. Plain
  words, "exact phrases", and prefix* work. Raw FTS5 syntax errors must
  never surface from user input. A query with no terms after
  sanitization is a usage error — `search` always needs terms;
  `--since/--until` only narrow a query, they cannot browse by date
  (that is a future `recent` verb's job).
- Output contract (agent-first, per good-practice rules):
  - Always a stable JSON envelope (never a bare array):
    `{"hits": [...], "total": N, "hint": null|"..."}`.
  - Default top 10, hard cap 50 (limit above cap clamps, does not error).
  - One compact hit per line with short keys:
    `p` (path), `id`, `s` (score), `date` (updated_at), `snip` (snippet,
    null for binary rows).
  - Snippet: FTS5 `snippet()` (token-based, centered near the match),
    post-truncated to a hard 200-character cap.
  - Full content is fetched with `memhub cat <path>`; search never dumps
    whole files.
  - When results are truncated, `total` carries the full count and
    `hint` states how to narrow or paginate.
- Narrowing flags: `--since/--until` (filter on `updated_at`, inclusive,
  accept `YYYY-MM-DD` or full ISO-8601), `--path <prefix>` (path prefix
  filter, component-aware), `--limit`, `--offset`.
- No matches: `{"hits": [], "total": 0, "hint": "..."}` with a hint
  listing top-level paths to steer the next query; exit 0 (no-match is
  not an error).

## Non-goals (v1)

- No vector/embedding search (sqlite-vec is the known later seam; at this
  scale lexical-first is the evidence-backed choice).
- No PDF/binary text extraction.
- No stemming (porter hurts exact identifiers, which memories are full
  of). No stopword removal.
- No recency/importance scoring blend.
- No `grep` verb yet.
- No search daemon, no network API — CLI only, like layer 1.

## Anti-goals

- Search must never dump large content into the agent's context by
  default (the context-rot lesson).
- Search must not mutate raw records — the FTS index is derived data,
  disposable and rebuildable.

## How it works (decided shape)

- Schema migration to `user_version = 2`: an FTS5 external-content table
  `memories_fts` (columns: `path`, `content`) with `content='memories'`,
  kept in sync by INSERT/UPDATE/DELETE triggers on `memories`. The `mv`
  command's path update flows through the UPDATE trigger automatically.
  The triggers feed `coalesce(content, '')` so binary rows are indexed
  by path. Migration runs inside `open_store` in a single transaction
  (atomic; interrupted migration rolls back).
- `open_store` refuses stores whose `user_version` is newer than the
  running code ("store was created by a newer memhub version") — this
  makes the cp-the-file merge workflow version-safe: a v2 store copied
  onto a machine still running layer-1 code errors clearly instead of
  corrupting the index.
- `memhub reindex` command: drops and rebuilds the FTS index from
  `memories` (the anti-goal "index is derived and disposable" needs a
  verb to be true). Also the recovery path for any index drift.
- Tokenizer: `unicode61 remove_diacritics 2` (builder call).
- Binary rows (content IS NULL) are indexed by path only (empty content
  in the index).
- Query sanitization: split user input into words and quoted phrases,
  escape FTS5 special characters, join with AND; each bare word gets
  prefix matching (`word*`). Empty after sanitization → usage error.
- Snippet via FTS5 `snippet()` with a marker the CLI strips (plain text
  snippet in output).
- Score: `-rank` (negate FTS5's negated BM25 so higher = better in JSON).
- `memhub init` on a fresh store creates v2 directly; existing v1 stores
  migrate on first open (builder call: migrate inside `open_store`).

## Left to the builder

- Exact sanitization rules and tests around FTS5 operator characters.
- Internal module layout (likely `search.py`) and test structure.

## Decided by the partner

- Search (FTS5/BM25) before grep.
- Focus on search first; PDF extraction deferred.
- JSON output stays (agent-first design), compact shape.
- Scale expectation: thousands of memories, GBs of data, mostly docs,
  occasional PDFs.

## Rulings by the controller (partner may override)

- Query-less date browsing is out of scope; search always needs terms.
- Layer-1 `put` widens text routing to any `text/*` content type.
- `memhub reindex` is added as a command.
- Output is always the `{"hits", "total", "hint"}` envelope — one stable
  shape for agents to rely on.
- v2 stores need NOT stay readable by older memhub builds; `open_store`
  refuses newer stores with a clear error instead.
- `--since/--until` filter `updated_at`, inclusive bounds.
