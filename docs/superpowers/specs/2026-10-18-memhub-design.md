# memhub — Layer 1 (raw store) design

Date: 2026-10-18
Status: approved by partner in brainstorm conversation

## Intent

memhub is a memory system for AI agents, built in layers. This document
covers **layer 1 only**: a raw, reliable store that memories land in and
stay in, untouched. Higher layers (keyword search, embeddings,
consolidation, recall) come later and will read from this layer; they
are not designed here.

The guiding metaphor, given by the partner: **think of it as a
filesystem.** Memories are files addressed by path, organized in
directories, held inside a SQLite database instead of on disk. v1
focuses on: write a file, read a file, directory support.

Why now: the partner wants a foundation for agent memory that is dumb
and dependable first, so that later intelligence has a solid source of
truth to sit on.

## What a memory is

Anything: something the user asked an agent to remember, a conversation
snippet extracted from an AI conversation, a documentation file, a
binary artifact. Layer 1 does not interpret content — it stores text or
bytes verbatim.

## Goals (v1)

- A Python CLI that operates on a SQLite database as the raw store.
- Filesystem-flavored operations, path-addressed:
  - `write` a memory at a path (content from argument, stdin, or a
    file path). Writing an existing path overwrites it, like a file.
  - `read` a memory by path.
  - `ls` a directory (and recursively) to list what's stored.
  - `rm` a memory (and, as a builder call, a whole subtree).
  - `init` to create a new store.
- Directory support: paths form a hierarchy; `ls` on a directory prefix
  lists its contents. Directories are implicit from path prefixes — no
  `mkdir` needed (builder call, mirrors git/URL semantics).
- Multiple stores: one SQLite file per context, e.g. `work.sqlite` on
  the work machine, `personal.sqlite` on the personal machine. Same
  schema, no cross-talk.
- Store selection via a config file (which store is active), with an
  environment variable override for one-off use. Resolution should feel
  like a shell resolving `$PWD` — no ceremony at call time. Concretely
  (builder call): one globally active store per machine, named in the
  config, since stores map to machines; `MEMHUB_DB` overrides for
  one-offs; `init` creates the store file and registers it in config.
- Machine-friendly output: JSON on stdout, so agents can parse results.
- Safe concurrent use by agents via SQLite's own mechanisms (WAL mode).

## Extension seams (must not be painted into a corner)

- Schema versioning via migrations, so later layers can attach to the
  same file (FTS5 tables, links between raw and derived records,
  embeddings columns or side tables).
- Content stored so both text and binary land faithfully (text column
  plus optional blob, or equivalent).
- Every record carries an immutable internal id, assigned at first
  write and preserved across overwrites (a delete + re-create yields a
  new id). Later layers link derived records to this id, not to the
  mutable path. (builder call)
- `cp` safety: the partner's merge workflow is copying the raw SQLite
  file, so every command uses a short-lived connection and checkpoints
  and truncates the WAL on exit — the database file is always safe to
  copy after a command finishes. (builder call)
- Envelope metadata kept thin but useful for later layers: at minimum
  path, timestamps (created/updated), size, and a content type hint.
  Kind/tags beyond that are builder's call — keep them minimal.
- Higher layers read raw records and never rewrite them; layer 1 stays
  the source of truth.

## Non-goals (v1)

- No retrieval intelligence: no search, no embeddings, no summarization.
- No export/import/merge commands. Cross-machine merging is `cp` of the
  SQLite file, overwriting — filesystem behavior is the guidance, and
  copying is deliberately *not* a feature.
- No servers, daemons, or network API. CLI only.
- No multi-user concerns; the partner is the only user.

## Anti-goals

- Layer 1 must not become smart: no interpretation, transformation, or
  silent rewriting of stored content.
- No policy/enforcement layer. The one-way flow the partner practices
  (personal memories may be copied into work, never the reverse) is a
  personal discipline, not a tool feature — the tool must not police it.
- No dependency sprawl: stdlib `sqlite3` ships SQLite; v1 should stay
  lean.

## Constraints

- Language: Python (partner's choice).
- Storage: SQLite (partner's choice), one file per store/context.
- Repo is fresh; Node tooling under `.pi/` is pi-agent config, not the
  product. Product code is Python.
- Config location/format, CLI argument library, packaging (e.g. `uv`,
  `pyproject.toml`), and exact JSON shapes are left to the builder.

## Decided by the partner

- Layered memory system; layer 1 = store only, SQLite as the filesystem.
- Content lands raw and stays verbatim; anything can be a memory.
- Python CLI as the interface agents use.
- Multiple databases, one per context (work vs personal).
- Config selects the active store.
- Merging personal → work is copying the whole file / overwriting;
  filesystem behavior is the guidance; no enforcement.
- v1 focus: write a file, read a file, directory support.
- Path-addressed memories with directory hierarchy as the organization
  (partner approved the path model over opaque ids + tags).

## Left to the builder

- Exact schema (suggested: single `memories` table keyed by path, with
  an immutable id, text/blob content columns, timestamps, size,
  content-type hint).
- Path semantics (builder call): paths are normalized to a leading-slash
  absolute form (`/a/b/c`), case-sensitive, no `.`/`..`/empty
  components, arbitrary other characters allowed. Following the
  filesystem guidance, a path cannot be both a memory and a directory
  prefix for other memories — writes that collide are rejected.
- Read contract (builder call): `read` streams raw content to stdout
  like `cat` (binary-safe); JSON metadata for a single record comes from
  `stat`, and `ls` emits JSON. This keeps agents' file reads byte-exact
  while preserving machine-readable output everywhere else.
- Whether overwrite keeps any history (suggested: no — overwrite is
  overwrite in v1; history is a later layer's problem).
- CLI command names and flags (suggested fs-style: `write`, `read`,
  `ls`, `rm`, `init`, maybe `cat` alias).
- Config format and location (suggested: `~/.memhub/config.toml` with a
  default store path plus named stores; `MEMHUB_DB` env override).
- Packaging, tests, and distribution details.
