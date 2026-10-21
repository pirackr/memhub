# memhub — Layer 2 (search) design

Date: 2026-10-18
Status: approved by partner 2026-10-18 (including the three flagged
defaults: full field names, reindex-based migration, unicode61 tokenizer).
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
PDFs. This is context for design choices, not a performance guarantee: the
spec makes no numeric promises about speed or index size.

## Goals (v1)

- `memhub search <query> [flags]` returning ranked hits as JSON.
- Indexed: the text content of memories, plus the path as a separate
  column weighted above content (a query matching the path scores
  higher). The path is indexed for EVERY row, including binaries — so
  PDFs are findable by path/name. Binary rows are path-only matches:
  `snippet` is null and `match` reports `"path"`. Content extraction
  for binaries is a deferred layer.
- Layer-1 amendment (proposed): `put` text routing becomes strict:
  - Explicit `text/*` content type: decoded as UTF-8 strictly; invalid
    UTF-8 is a usage error. Content fills the `content` column.
  - Explicit non-text (binary) content type: honored as-is; no sniff
    override.
  - No content type given: existing sniff behavior (valid UTF-8 →
    `text/plain`, else `application/octet-stream`).
  Raw storage stays verbatim; only the text/blob routing widens.
- Ranking: SQLite FTS5 Okapi-BM25 (`ORDER BY rank`). No recency blending
  in v1; dates ride along on every hit.
- Query syntax: bare words are exact tokens by default; prefix matching
  only via an explicit trailing `*` (`word*`); `"quoted phrases"` are
  supported. Invalid or unclosed syntax (unbalanced quote, stray FTS5
  operator) is a clear memhub error explaining the syntax — it never
  silently broadens the query. Raw SQL/FTS5 error text never leaks to
  the user. A query with no terms after parsing is a usage error —
  `search` always needs terms; `--since/--until` only narrow a query,
  they cannot browse by date.
- Output contract (agent-first):
  - Always a stable JSON envelope (never a bare array):
    `{"hits": [...], "total": N, "hint": null|"..."}`.
  - Default top 10. `--limit` must be in 1..50 (outside that range is a
    usage error, not a clamp); negative `--offset` is a usage error.
  - Hits use clear full field names (proposed default, not yet approved
    by the partner): `path`, `id`, `score`, `updated_at`, `snippet`,
    `match` (`"content"` | `"path"`).
  - Snippet: FTS5 `snippet()` called with empty markers — no marker
    characters in the output and no post-hoc stripping that could remove
    arbitrary user text. Plain text, no invented tags. Hard 200-character
    cap; when shortening, the window is centered on the match so the
    matching passage is always preserved. Path-only matches: `snippet`
    is null and `match` is `"path"`.
  - Full content is fetched with `memhub cat <path>`; search never dumps
    whole files.
  - `total` carries the full hit count; `hint` is short and bounded (a
    fixed small cap on listed paths) and states how to narrow or
    paginate.
- Narrowing flags: `--since/--until` (filter on `updated_at` — the
  record's last-write time, not an event date), `--path <prefix>`
  (component-aware path prefix), `--limit`, `--offset`. Date rules:
  - All inputs are normalized to UTC. A full timestamp must carry a
    timezone (`Z` or `±hh:mm`); a bare timestamp without one is an error.
  - Date-only `--since` means the beginning of that UTC day; date-only
    `--until` covers the whole day, implemented internally as an
    exclusive next-midnight bound. Full-timestamp bounds are inclusive.
  - A reversed range (`since > until`) is a usage error.
- Result order is stable: score descending, then path ascending, then id
  ascending. Pagination offsets are not a snapshot — separate calls can
  observe different data if the store changed between them.
- No matches: `{"hits": [], "total": 0, "hint": "..."}` with a bounded
  hint listing top-level paths to steer the next query; exit 0
  (no-match is not an error).

## Non-goals (v1)

- No vector/embedding search (sqlite-vec is the known later seam; at this
  scale lexical-first is the evidence-backed choice).
- No PDF/binary text extraction.
- No stemming (porter hurts exact identifiers, which memories are full
  of). No stopword removal.
- No recency/importance scoring blend.
- No date-only browsing (search always requires terms; a future `recent`
  verb's job).
- No `grep` verb yet.
- No search daemon, no network API — CLI only, like layer 1.

## Anti-goals

- Search must never dump large content into the agent's context by
  default (the context-rot lesson).
- Search must not mutate raw records — the FTS index is derived data,
  disposable and rebuildable.

## How it works (proposed shape)

Schema v2 (`user_version = 2`):

- FTS5 external-content table `memories_fts` (columns `path`, `content`)
  with `content='memories'`, joined on `memories`' implicit INTEGER
  rowid. Note: `memories.id` is a TEXT uuid and must NOT be used as the
  FTS rowid; triggers pass `new.rowid`/`old.rowid`.
- INSERT/UPDATE/DELETE triggers on `memories` keep the index in sync,
  feeding `coalesce(content, '')` so binary rows are indexed by path.
  The `mv` command's path update flows through the UPDATE trigger
  automatically.
- Fresh `memhub init` creates v2 directly.

Version and migration policy:

- `open_store` refuses stores whose `user_version` is newer than the
  running code ("store was created by a newer memhub version"). This
  protects new code reading newer stores only.
- No implicit migration on open: ordinary layer-1 operations
  (put/cat/ls/rm/mv) remain fully usable on v1 stores, and `search` on a
  v1 store fails with an actionable error telling the user to run
  `memhub reindex`. This avoids building a gigabyte-scale index as a
  side effect of, say, `cat`.
- `memhub reindex` is the single migration and rebuild command. On a v1
  store it creates the FTS table and triggers, backfills, and bumps
  `user_version` to 2. On a v2 store it drops and rebuilds the index —
  the recovery path for drift, which makes the "index is derived and
  disposable" anti-goal true.
- Concurrency and atomicity: `reindex` acquires the write lock
  (`BEGIN IMMEDIATE`), then re-checks `user_version` after acquiring it,
  so concurrent migrations serialize (the loser sees version 2 and no-ops
  or rebuilds). Schema + triggers + backfill + version bump run in one
  transaction; any failure rolls back completely and the index is never
  half-built. Progress is reported on stderr.
- Backfill of existing rows: rows with `content_type LIKE 'text/%'` and
  `content IS NULL` move blob → content, preserving the exact bytes, id,
  created_at and updated_at. An existing `text/*` row whose blob is not
  valid UTF-8 keeps its bytes in the blob column, is indexed by path
  only, and is counted and reported on stderr — no silent loss. No PDF
  extraction.

Version safety for the cp-the-file merge workflow:

- The `open_store` version check lives in the new code and cannot
  retroactively protect old v1 clients: old builds have no version check
  in `open_store` and will NOT reliably reject a v2 store — there is no
  false-rejection guarantee. Upgrade memhub on both machines before
  sharing v2 stores; old clients against v2 stores are unsupported.

Index details:

- Tokenizer: `unicode61 remove_diacritics 2` (proposed default).
- Binary rows (content IS NULL) are indexed by path only (empty content
  in the index).
- Score: `-rank` (negate FTS5's negated BM25 so higher = better in JSON).

## Evaluation

The spec makes no numeric performance or index-size promises. Retrieval
quality (accuracy) evaluation for this search layer is maintained
OUTSIDE this repository, in a separate benchmark workspace
(`memhub-bench`), and drives future tuning. Nothing in this spec
promises numeric results.

## Decided by the partner

- Search (FTS5/BM25) before grep.
- Focus on search first; PDF extraction deferred.
- JSON output stays (agent-first design).
- Scale expectation: thousands of memories, GBs of data, mostly docs,
  occasional PDFs.

## Engineering defaults (approved by partner 2026-10-18)

- Full field names in hits (`path`, `id`, `score`, `updated_at`,
  `snippet`, `match`) instead of the earlier cryptic short keys
  (`p`, `s`, `snip`).
- Exact tokens by default; prefix only via explicit trailing `*`.
- Date normalization rules (UTC normalization, date-only `--until` as
  exclusive next-midnight, inclusive timestamp bounds).
- Reject out-of-range `--limit`/`--offset` rather than clamping.
- Tokenizer `unicode61 remove_diacritics 2`.
- Migration/reindex strategy (explicit `reindex`, no implicit migration
  in `open_store`).

## Rulings by the controller (partner may override)

- Query-less date browsing is out of scope; search always needs terms.
- Layer-1 `put` adopts the strict text-routing rules above (any explicit
  `text/*` decoded strictly; explicit binary honored; sniff only when no
  type is given).
- `memhub reindex` is the only migration/rebuild path; `open_store` does
  not migrate implicitly.
- Output is always the `{"hits", "total", "hint"}` envelope — one stable
  shape for agents to rely on.
- v2 stores need NOT stay readable by older memhub builds; the version
  check protects new code only, and old clients against v2 stores are
  unsupported (upgrade both machines before sharing).
- `--since/--until` filter `updated_at` with the date rules above.

## Left to the builder

- Exact sanitization rules and tests around FTS5 operator characters.
- Internal module layout (likely `search.py`) and test structure.
