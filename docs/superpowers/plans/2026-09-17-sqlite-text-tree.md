# SQLite Text Tree Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to execute task-by-task. Track each checkbox; review each task before proceeding.

**Goal:** Build the specified Python SQLite text vault, seven-command CLI, portable agent skill, and reproducible correctness/performance evaluation.

**Architecture:** Keep canonical content in an indexed adjacency-list tree, accessed through a context-managed storage library. Separate path/text validation, transactional document operations, host import/decoding, and CLI presentation so each boundary is independently testable. Skills and benchmarks consume the same public interfaces rather than bypassing storage semantics.

**Tech Stack:** Python 3.11+, standard-library SQLite/argparse, chardet 5.2.0 loaded only for legacy decoding, pytest, and setuptools packaging.

**Spec:** `docs/superpowers/specs/2026-09-17-sqlite-text-tree-design.md`; read it alongside this plan.

## Files

All listed files are new; subsequent tasks modify them after their creation. The existing spec and local `.pi/` configuration remain unchanged.

- Create `pyproject.toml` — package, dependencies, console entry point, pytest configuration.
- Create `.gitignore` — interpreter/build caches, disposable vaults, and generated benchmark corpora.
- Create `README.md` — installation, seven-command reference, limitations, and verification instructions.
- Create `src/memhub/__init__.py` — explicit public library exports, without eager detector imports.
- Create `src/memhub/__main__.py` — module invocation of the CLI.
- Create `src/memhub/errors.py` — typed failures, stable codes, and exit statuses.
- Create `src/memhub/models.py` — shared immutable result and input records.
- Create `src/memhub/schema.sql` — root, adjacency list, indexes, constraints, and triggers.
- Create `src/memhub/vault.py` — exclusive creation, validated opening, transactions, and connection lifetime.
- Create `src/memhub/paths.py` — virtual path validation and normalization.
- Create `src/memhub/text.py` — content admission and UTF-8 hashing.
- Create `src/memhub/tree.py` — indexed resolution, parent creation, listing, and subtree removal.
- Create `src/memhub/documents.py` — document metadata, reads, conditional writes, and transactional edits.
- Create `src/memhub/edits.py` — edit-input validation and original-text replacement planning.
- Create `src/memhub/encoding.py` — BOM handling, UTF-8 detection, and strict legacy decoding.
- Create `src/memhub/sources.py` — descriptor-based, symlink-safe host traversal.
- Create `src/memhub/importer.py` — streaming, atomic import and encoding summaries.
- Create `src/memhub/cli.py` — argument parsing, dispatch, stdout/stderr contracts.
- Create `tests/conftest.py` — isolated vault/source fixtures and invariant assertions.
- Create `tests/test_vault.py` — identity, schema, initialization, and connection tests.
- Create `tests/test_paths_text.py` — path, Unicode, admission, and hash tests.
- Create `tests/test_tree.py` — structural constraints, traversal, listings, deletion.
- Create `tests/test_documents.py` — read/write conditions, ranges, metadata, timestamps.
- Create `tests/test_edits.py` — replacement matching, conflicts, and atomic integration.
- Create `tests/test_encoding.py` — codec selection and confidence behavior.
- Create `tests/test_sources.py` — host traversal, exclusions, and race rejection.
- Create `tests/test_importer.py` — mappings, warnings, streaming, and rollback.
- Create `tests/test_concurrency.py` — contention, conditional races, and crash recovery.
- Create `tests/test_cli.py` — subprocess behavior for all commands and statuses.
- Create `tests/test_skill.py` — skill structure and executable documented examples.
- Create `tests/test_benchmarks.py` — dataset, reference backend, metrics, and report validation.
- Create `skills/memhub/SKILL.md` — portable operational guidance.
- Create `skills/memhub/references/commands.md` — tested examples and troubleshooting.
- Create `benchmarks/__init__.py` — benchmark module namespace.
- Create `benchmarks/corpus.py` — seeded text corpus and manifests.
- Create `benchmarks/filesystem.py` — ordinary-file comparison backend.
- Create `benchmarks/run.py` — equivalent workloads, measurements, and JSON reports.
- Create `benchmarks/README.md` — reproducible execution and interpretation.
- Create `benchmarks/agent-trials.md` — opt-in live-agent scenarios and scoring.
- Create `benchmarks/results/v1-small.json` — checked small-run measurements.
- Create `benchmarks/results/v1-scale.json` — checked 100,000-document measurements.
- Create `benchmarks/results/agent-trials.json` — actual trial outcomes or explicit untested records.

## Global Constraints

The following requirements are copied verbatim from the spec; all tasks inherit them.

- Five everyday commands: `ls`, `read`, `write`, `edit`, and `rm`.
- Two setup/ingestion commands: `init` and `import`.
- **Search is entirely deferred to v2.** V1 has no `search`, `grep`, FTS5 index, regex search, or search benchmark.
- V1 also excludes `mkdir`, `stat`, `find`, and `mv` commands; mounting/FUSE; checkout/synchronization; MCP; agent tool overrides; executable workspaces; binaries; symlinks/hard links; per-document permissions; tags; revisions/history; embeddings; semantic links; and memory lifecycle features.
- No daemon or background indexing process is required. No knowledge content is mirrored to ordinary files.
- Paths are absolute and use `/` independently of the host OS.
- Names are case-sensitive, with no Unicode normalization or locale-dependent comparison.
- Normal operations never initialize or migrate a vault implicitly.
- Use rollback-journal mode (`DELETE`) with `synchronous=FULL` for v1.
- Every connection enables foreign keys and a bounded five-second busy timeout.
- The host vault file's OS permissions are the access boundary.
- No replacement-character or ignore-errors decoding is allowed.
- Directory import is one transaction: preserve relative paths and empty directories, merge directories, and refuse existing file destinations.
- Success reporting happens only after commit.
- Maintain one canonical portable skill at `skills/memhub/SKILL.md`, using standard `name` and `description` frontmatter.
- Tests run only against disposable fixtures.
- The v1 benchmark target is **100,000 documents and approximately 1 GB of input UTF-8 text**, not a guarantee that the resulting SQLite file stays below 1 GB.
- There is no universal speed-parity claim or arbitrary pre-measurement latency threshold.

## Execution conventions

Each checkbox represents about 2–5 minutes of active work; full benchmark runs can take longer unattended. Run targeted pytest tests for the named test file after each implementation slice; run the accumulated suite before each commit. A red test must fail for the stated missing behavior, not an unrelated environment problem. For regression tests that already pass, record that evidence and do not manufacture a failure.

Public storage operations normalize paths before resolution; transaction helpers consume canonical paths and never commit independently. Result records are immutable dataclasses in `src/memhub/models.py`. Commit only the files listed for the current task.

## Tasks

### 1. Create and reopen a valid vault

**Files:** Create `pyproject.toml`, `.gitignore`, `src/memhub/__init__.py`, `src/memhub/errors.py`, `src/memhub/schema.sql`, `src/memhub/vault.py`, `tests/conftest.py`, and `tests/test_vault.py`.

**Interfaces:** Consumes a host `Path`. Produces `create_vault(path)`, returning nothing, and `open_vault(path)`, returning context-managed `Vault` with `path`, SQLite `connection`, and `transaction(write=False)` yielding that connection. `MemhubError` carries string `code`/`message` and integer `exit_status`; subclasses `InvalidInput`, `Missing`, `Conflict`, `Unsupported`, `Busy`, and `VaultFailure` map to statuses 2–7 respectively.

- [ ] Configure the package resource for `schema.sql`, pytest, and isolated fixtures; pin chardet without importing it at package load.
- [ ] Write tests for exclusive creation, missing host parent, one root, reopen, unrelated databases, unsupported versions, and rollback when a transaction raises.
- [ ] Run `tests/test_vault.py`; confirm the lifecycle API is missing.
- [ ] Implement exclusive host-file creation, UTF-8 schema initialization, root insertion, and cleanup restricted to the file this invocation created.
- [ ] Implement validated opening using SQLite application ID 0x4D454D48 and user version 1; check required schema objects and never use create-on-open mode.
- [ ] Implement connection pragmas, SQLite row objects, read/immediate-write transactions, cleanup, and typed SQLite/host failure translation.
- [ ] Verify lifecycle tests and an installed-package resource smoke test pass.
- [ ] Commit as “feat: create validated SQLite vaults”.

### 2. Define canonical paths and admitted text

**Files:** Create `src/memhub/paths.py`, `src/memhub/text.py`, `tests/test_paths_text.py`; modify `src/memhub/errors.py`.

**Interfaces:** Consumes strings; produces `normalize_path(path)` returning a canonical string, `validate_text(content)` returning nothing, and `content_hash(content)` returning a SHA-256 hexadecimal string.

- [ ] Write cases for repeated separators, root, dot components, relative paths, control names, Unicode surrogates, literal glob characters, case distinctions, and composed/decomposed names.
- [ ] Add content cases for allowed whitespace, rejected NUL/C0 controls, literal U+FFFD, preserved CRLF, and hashing complete UTF-8 bytes.
- [ ] Run `tests/test_paths_text.py`; confirm these functions are absent.
- [ ] Implement separator-only normalization and strict name validation without host expansion or Unicode normalization.
- [ ] Implement content admission and hashing without rewriting text.
- [ ] Verify the targeted and accumulated tests pass.
- [ ] Commit as “feat: validate virtual paths and text”.

### 3. Enforce and resolve the directory tree

**Files:** Create `src/memhub/tree.py`, `tests/test_tree.py`; modify `src/memhub/schema.sql` and `tests/conftest.py`.

**Interfaces:** Consumes a SQLite connection and normalized virtual path. Produces `resolve(connection, path)` and `ensure_directories(connection, path)`, each returning `sqlite3.Row`; neither starts or commits a transaction.

- [ ] Write direct-SQL rejection cases for a second root, missing/file parents, duplicate siblings, invalid kinds/content/names, self-parenting, and changed IDs/names/parents/kinds.
- [ ] Add nested resolution, hidden-name, empty-directory, and rollback-of-created-parents cases.
- [ ] Run `tests/test_tree.py`; confirm invalid structures or missing resolution functions fail the assertions.
- [ ] Add root-specific checks, the unique sibling index, parent/type/name triggers, and immutable-link/root protection.
- [ ] Implement indexed component resolution and parent creation with UTC timestamps and direct-parent membership updates only.
- [ ] Verify constraints, rollback, foreign-key checks, and SQLite integrity pass.
- [ ] Commit as “feat: enforce indexed tree invariants”.

### 4. Write documents atomically

**Files:** Create `src/memhub/models.py`, `src/memhub/documents.py`, `tests/test_documents.py`; modify `src/memhub/__init__.py`.

**Interfaces:** Consumes `Vault`, path/content strings, an optional hash string, and an absence-condition boolean. Produces `Entry` with integer `id`, string `path`, `kind` of file/directory, optional integer `size_bytes`, and UTC strings `created_at`/`updated_at`; directories use null size. `WriteResult` contains `entry: Entry` and string `content_hash`. `write_file(vault, path, content, if_match=None, if_absent=False)` and `write_in_transaction(connection, path, content, if_match=None, if_absent=False)` return `WriteResult`; the latter requires an existing write transaction.

- [ ] Write cases for new parents, empty content, overwrite, preserved creation time, byte size, direct-parent timestamps, directory collisions, stale hashes, and mutually exclusive conditions.
- [ ] Run `tests/test_documents.py`; confirm writes are unavailable.
- [ ] Define the result records; validate admitted content and implement unconditional writes through the caller-owned transactional helper.
- [ ] Check absence/hash conditions after acquiring the write reservation; reject a missing expected document and roll back all created parents on failure.
- [ ] Verify results, original bytes, hashes, timestamps, and rollback assertions.
- [ ] Commit as “feat: add atomic conditional document writes”.

### 5. Read whole documents and line ranges

**Files:** Modify `src/memhub/models.py`, `src/memhub/documents.py`, `src/memhub/__init__.py`, and `tests/test_documents.py`.

**Interfaces:** Consumes `Vault`, a path string, integer start line, and optional integer line count. Produces `ReadResult` with `entry: Entry`, string `content`/`content_hash`, integer `start_line`, optional integer `end_line`, and boolean `has_more`. `read_file(vault, path, start_line=1, lines=None)` returns it; empty ranges have null `end_line`, and hashing covers the complete document.

- [ ] Write cases for whole/partial reads, CRLF, no final newline, empty files, past-end ranges, invalid range arguments, directories, and missing paths.
- [ ] Run the read cases in `tests/test_documents.py`; confirm missing read behavior.
- [ ] Implement snapshot reads and LF-only slicing with no text normalization or artificial trailing empty line.
- [ ] Return consistent range metadata, complete-content hashes, and UTF-8 sizes.
- [ ] Verify exact returned bytes and all document tests.
- [ ] Commit as “feat: read document ranges with metadata”.

### 6. List directories with bounded pagination

**Files:** Modify `src/memhub/models.py`, `src/memhub/tree.py`, `src/memhub/__init__.py`, and `tests/test_tree.py`.

**Interfaces:** Consumes `Vault`, path string, recursive boolean, positive integer limit, and nonnegative integer offset. Produces `ListResult` with `entries: list[Entry]`, boolean `has_more`, and optional integer `next_offset`; `list_entries(vault, path="/", recursive=False, limit=100, offset=0)` returns it.

- [ ] Write immediate/recursive listings with Unicode, dot names, empty directories, more than 100 children, last pages, and invalid file/range inputs.
- [ ] Run the listing cases; confirm the public operation is missing.
- [ ] Implement snapshot traversal with computed paths, binary ordering, byte sizes, and one look-ahead entry to determine continuation.
- [ ] Keep directory contents out of results, exclude the selected directory itself, and omit a continuation offset on the final page.
- [ ] Verify pagination and query plans use the parent/name index for component lookup.
- [ ] Commit as “feat: add bounded directory discovery”.

### 7. Validate and apply exact-text replacements

**Files:** Create `src/memhub/edits.py`, `tests/test_edits.py`; modify `src/memhub/models.py`.

**Interfaces:** Consumes decoded JSON for parsing, or an original string and sequence of edits for replacement. Produces `Edit` with string `old_text`/`new_text` and boolean `replace_all=False`; `parse_edits(payload)` returns `list[Edit]`, and `apply_edits(original, operations)` returns an admitted string.

- [ ] Write malformed-input, empty-list/needle, no-match, ambiguous-match, replace-all, Unicode, overlap, and original-versus-incremental matching cases.
- [ ] Run `tests/test_edits.py`; confirm parser and replacement behavior are absent.
- [ ] Implement strict field/type checks, rejecting unknown fields and nonboolean replacement flags.
- [ ] Select left-to-right non-overlapping occurrences per operation, reject intersecting selections across operations, splice from the original text, and validate the result.
- [ ] Verify all pure edit cases pass, including rejection of edits introducing prohibited controls.
- [ ] Commit as “feat: implement exact atomic edit planning”.

### 8. Commit edits with conditional-write protection

**Files:** Modify `src/memhub/documents.py`, `src/memhub/__init__.py`, and `tests/test_edits.py`.

**Interfaces:** Consumes `Vault`, path, a sequence of `Edit`, and optional string `if_match`; produces `edit_file(vault, path, operations, if_match=None)` returning the existing `WriteResult`.

- [ ] Write stale-hash, missing-file, overlapping-operation, successful multi-edit, and unchanged-content/timestamp-on-error integration cases.
- [ ] Run the edit integration cases; confirm the transactional entry point is missing.
- [ ] Acquire the write reservation, resolve existing content, check the hash, and apply the pure replacement function inside that transaction.
- [ ] Persist through `write_in_transaction` and return the resulting hash/metadata only after commit.
- [ ] Verify rollback/integrity and accumulated tests.
- [ ] Commit as “feat: commit conflict-safe document edits”.

### 9. Remove entries without damaging neighboring trees

**Files:** Modify `src/memhub/tree.py`, `src/memhub/__init__.py`, and `tests/test_tree.py`.

**Interfaces:** Consumes `Vault`, path string, and recursive boolean. Produces `remove_entry(vault, path, recursive=False)`, returning nothing and raising typed errors for missing paths, unauthorized nonempty-directory removal, or root deletion.

- [ ] Write file, empty-directory, protected-root, guarded-subtree, sibling-preservation, and greater-than-1,000-level deletion cases.
- [ ] Add a failure-injection case asserting that partially executed deletion rolls back and surviving ancestor timestamps are unchanged.
- [ ] Run deletion cases; confirm removal is unavailable.
- [ ] Collect descendants with SQL and delete bottom-up without Python recursion or cascading-trigger depth dependence; update the surviving direct parent in the same transaction.
- [ ] Verify foreign keys, integrity, deep deletion, and accumulated tests.
- [ ] Commit as “feat: add guarded subtree removal”.

### 10. Detect encodings without silently replacing text

**Files:** Create `src/memhub/encoding.py`, `tests/test_encoding.py`; modify `src/memhub/models.py`.

**Interfaces:** Consumes source bytes. Produces `DecodedText` with string `text`/`encoding` and float `confidence`; `decode_bytes(data)` returns that record. Report canonical codec labels; deterministic BOM/UTF-8 cases use confidence 1.0.

- [ ] Write BOM-order, UTF-8, legacy-codec, uncertain-guess, unknown-codec, decode-failure, literal-U+FFFD, newline, and binary-admission cases.
- [ ] Run `tests/test_encoding.py`; confirm decoding is absent.
- [ ] Implement UTF-32-before-UTF-16 BOM dispatch and strict UTF-8 decoding, consuming only a signature BOM.
- [ ] Lazy-load pinned chardet for the remaining cases, decode the entire file strictly, and apply shared text admission without replacement/ignore modes.
- [ ] Verify deterministic fake-detector cases, real legacy fixtures, and that ordinary library imports do not load chardet.
- [ ] Commit as “feat: decode imported text automatically”.

### 11. Traverse host sources without following links

**Files:** Create `src/memhub/sources.py`, `tests/test_sources.py`; modify `src/memhub/models.py`.

**Interfaces:** Consumes a host `Path` and set of excluded host `Path` values. Produces `SourceItem` with string `source_path`/`relative_path`, file/directory `kind`, and optional bytes `data`; `iter_sources(source, blocked_paths)` yields these incrementally. The root has empty relative path; directories have null data.

- [ ] Write regular-file, nested/empty-directory, symlink-ancestor/leaf, FIFO, blocked-vault, and source-replaced-after-enumeration cases.
- [ ] Run `tests/test_sources.py`; confirm the safe iterator is absent.
- [ ] Open path components relative to verified directory descriptors with no-follow flags; verify the opened object's type before reading, including protection against FIFO blocking.
- [ ] Implement iterative traversal and descriptor cleanup; reject blocked path/inode matches and platforms lacking the required safe-open primitives rather than silently following links.
- [ ] Verify replacement-race tests and incremental consumption without corpus buffering.
- [ ] Commit as “feat: traverse import sources safely”.

### 12. Import a tree as one transaction

**Files:** Create `src/memhub/importer.py`, `tests/test_importer.py`; modify `src/memhub/models.py` and `src/memhub/__init__.py`.

**Interfaces:** Consumes `Vault`, host `Path`, and destination string. Produces `ImportWarning` with strings `source_path`/`encoding`/`message` and float `confidence`; `ImportResult` has integers `files`/`directories` and `encoding_counts: dict[str, int]`, counting new directories only. `import_source(vault, source, destination, on_warning=None)` returns `ImportResult`; the optional callback consumes one `ImportWarning` and returns nothing.

- [ ] Write exact-file mapping, directory-content merging, empty-directory, encoding-summary, and below-0.80-warning cases.
- [ ] Add collision, unsupported-source, mid-import I/O/decode failure, and active-vault/journal exclusion cases, asserting whole-tree rollback.
- [ ] Run `tests/test_importer.py`; confirm import is unavailable.
- [ ] Within one write transaction, consume source items, map virtual paths, preserve directories, and decode/write one file at a time using conditional creation.
- [ ] Emit uncertain-decoding warnings through the callback, accumulate only counts, and return success after commit; do not retain document bodies or warning lists.
- [ ] Verify rollback, byte equality, integrity, and bounded iterator consumption.
- [ ] Commit as “feat: import text trees atomically”.

### 13. Verify contention and interrupted-write recovery

**Files:** Create `tests/test_concurrency.py`; modify `src/memhub/vault.py` and `src/memhub/errors.py` where the new regressions expose missing translation or cleanup.

**Interfaces:** Consumes existing transaction and mutation APIs; produces stable `Busy` errors for exhausted lock waits and preserves all public signatures.

- [ ] Write synchronized two-process write/edit races using the same initial hash; require exactly one differing update to succeed.
- [ ] Add a held-lock timeout case and a child process killed after an uncommitted write, followed by reopen/content/integrity checks.
- [ ] Run `tests/test_concurrency.py`; record actual failures, including any incorrect busy classification.
- [ ] Correct busy/locked exception mapping and failed-commit rollback/connection cleanup without retrying outside the configured timeout or deleting recovery journals.
- [ ] Verify approximately five-second contention behavior with scheduling tolerance and all crash/race cases.
- [ ] Commit as “test: verify concurrent writes and crash recovery”.

### 14. Expose initialization, listing, and reading through the CLI

**Files:** Create `src/memhub/cli.py`, `src/memhub/__main__.py`, `tests/test_cli.py`; modify `pyproject.toml`.

**Interfaces:** Consumes an optional argument-string list. Produces `main(argv=None)` returning an integer exit status and the installed `memhub` command. JSON uses `api_version: 1`, boolean `ok`, and `result` or `error`; error objects have string `code`/`message`, and success payloads reuse library record fields.

- [ ] Write subprocess cases for explicit vault selection, help, `init`, `ls`, raw `read`, ranges, missing paths, parser failures, and JSON stderr errors.
- [ ] Run `tests/test_cli.py`; confirm the console interface is absent.
- [ ] Register the entry point and implement global options before subcommands, including parser-error conversion when JSON was requested.
- [ ] Dispatch the three commands and serialize metadata without changing plain-read bytes, encoding, or final newlines.
- [ ] Map known errors to their statuses and unexpected exceptions to status 1 without tracebacks or document disclosure.
- [ ] Verify both installed-console and module invocations.
- [ ] Commit as “feat: expose vault navigation CLI”.

### 15. Expose writes, edits, and removal through the CLI

**Files:** Modify `src/memhub/cli.py` and `tests/test_cli.py`.

**Interfaces:** Consumes strict UTF-8 stdin for `write` and decoded JSON arrays for `edit`; produces the existing write/edit result records and an empty successful removal result.

- [ ] Write subprocess cases for multiline Unicode/CRLF input, invalid UTF-8, both write conditions, stale edits, malformed edit JSON, and guarded recursive removal.
- [ ] Run mutation cases; confirm the commands are rejected before implementation.
- [ ] Add write/edit argument parsing and stdin decoding; route edit arrays through `parse_edits` and conditions through the library.
- [ ] Add removal dispatch and ensure failures write no success record or raw document content to stdout.
- [ ] Verify statuses 2–5 and all mutation outputs against library results.
- [ ] Commit as “feat: expose safe document mutations”.

### 16. Expose import and finalize diagnostic contracts

**Files:** Modify `src/memhub/cli.py`, `tests/test_cli.py`, and `src/memhub/__init__.py`.

**Interfaces:** Consumes host source plus virtual destination and the import callback; produces `ImportResult` on stdout after commit and one versioned warning object per stderr line in JSON mode.

- [ ] Write subprocess cases for import summaries, successful warnings, failed-import rollback, busy status 6, schema/I/O status 7, and absence of all excluded commands.
- [ ] Run these cases; confirm import dispatch and warning rendering are absent.
- [ ] Implement import arguments and warning rendering with source/encoding/confidence, keeping diagnostics separate from stdout.
- [ ] Finish the explicit public export list and audit all seven commands for identical error-envelope and exit-status behavior.
- [ ] Verify normal package/CLI startup still avoids detector imports and run the accumulated suite.
- [ ] Commit as “feat: complete import and diagnostic CLI contracts”.

### 17. Ship tested agent guidance

**Files:** Create `README.md`, `skills/memhub/SKILL.md`, `skills/memhub/references/commands.md`, `benchmarks/agent-trials.md`, and `tests/test_skill.py`.

**Interfaces:** Consumes the seven-command CLI; produces portable skill frontmatter, runnable examples, and fixed live-trial scenarios with expected vault outcomes.

- [ ] Write checks for required frontmatter, command availability, and examples performing conditional creation, paged discovery, ranged reading, editing, conflict recovery, and authorized deletion.
- [ ] Run `tests/test_skill.py`; confirm the documentation artifacts are absent.
- [ ] Write the short skill with explicit vault selection, shell-only access, safe stdin, fresh-hash workflows, stored-text-as-data guidance, and no search or automatic configuration changes.
- [ ] Write detailed command/error examples and installation instructions for Claude Code, OpenCode, and pi; document permissions, hash limitations, journals, import atomicity, and encoding uncertainty.
- [ ] Define opt-in live scenarios, expected state, version/tool-call/error recording, and untested outcomes when credentials or harnesses are unavailable.
- [ ] Execute the documented examples in temporary vaults and verify the skill checks.
- [ ] Commit as “docs: teach agents the minimal vault workflow”.

### 18. Generate deterministic benchmark corpora

**Files:** Create `benchmarks/__init__.py`, `benchmarks/corpus.py`, `tests/test_benchmarks.py`, and `benchmarks/README.md`.

**Interfaces:** Consumes a host root `Path` and integer count/byte-target/seed. Produces `CorpusManifest` with integers `seed`/`document_count`/`input_bytes` and `documents: dict[str, str]` mapping relative paths to hashes; `generate_corpus(root, count, target_bytes, seed)` returns it without retaining document bodies. This benchmark-only record lives in `benchmarks/corpus.py`.

- [ ] Write seed-repeatability, count/byte-budget, Unicode, mixed-size, wide/deep-path, and manifest-hash tests using small fixtures.
- [ ] Run corpus cases in `tests/test_benchmarks.py`; confirm generation is missing.
- [ ] Implement incremental UTF-8 fixture generation; define small as 1,000 documents/10,000,000 bytes and scale as 100,000 documents/1,000,000,000 bytes.
- [ ] Document both profiles and keep source corpora/vaults ignored while report JSON remains trackable.
- [ ] Verify repeatability and generated content independently.
- [ ] Commit as “test: generate reproducible text-tree datasets”.

### 19. Build the ordinary-files comparison backend

**Files:** Create `benchmarks/filesystem.py`; modify `tests/test_benchmarks.py` and `benchmarks/README.md`.

**Interfaces:** Consumes a host root `Path`. Produces context-managed `FilesystemStore(root)` exposing `list_entries`, `read_file`, `write_file`, `edit_file`, `remove_entry`, and `import_source`, with the library's arguments after the vault parameter and its result types. Exclude internal IDs/native timestamps from comparisons; content, paths, ranges, sizes, and continuation must match.

- [ ] Write shared small-workload assertions against SQLite and the missing filesystem backend for listing, reads, conditional changes, deletions, and ingestion.
- [ ] Run the comparison cases; confirm the reference backend is missing.
- [ ] Implement sorted/paged listing and exact ranged reads with equivalent metadata and hashing work.
- [ ] Implement serialized mutations using same-directory temporary writes, atomic replacement, file/directory flushing, and condition checks under a lock stored outside the measured tree.
- [ ] Implement subtree removal and bulk copy; label missing multi-file rollback and native timestamp differences rather than inventing filesystem guarantees.
- [ ] Verify content/range/pagination equivalence, newline preservation, and documented durability/locking behavior.
- [ ] Commit as “test: add durable filesystem benchmark reference”.

### 20. Measure library and CLI workloads separately

**Files:** Create `benchmarks/run.py`; modify `benchmarks/filesystem.py`, `tests/test_benchmarks.py`, and `benchmarks/README.md`.

**Interfaces:** Consumes profile string small/scale, integer seed, and output `Path`. Produces `run_profile(profile, seed, output)`, returning/writing a JSON-compatible report with version, environment, corpus summary, raw samples, statistics, resources, checks, and caveats. `validate_report(report, expected_count, expected_bytes)` accepts that dictionary and two integers, returning nothing or raising `ValueError`. Benchmark-only `benchmarks.filesystem.main(argv=None)` accepts Memhub-equivalent arguments and returns an exit integer.

- [ ] Write fake-clock median/nearest-rank-p95 tests and small-run assertions for every specified operation, both modes, mutation reset, required metadata, and mismatched-corpus rejection.
- [ ] Run runner tests; confirm measurement/report interfaces are missing.
- [ ] Implement open-store timing with 30 samples per ordinary operation and three bulk samples; check results outside timing, reset mutation fixtures, and disclose bulk percentile uncertainty.
- [ ] Add fresh-process CLI timing for Memhub and an equivalent Python filesystem adapter, using identical serialization/output limits and including startup costs.
- [ ] Collect median/p95, counts, throughput, peak process memory, database/journal and filesystem usage; record Python/SQLite/detector versions, OS/filesystem, hardware, seed, and durability settings.
- [ ] Separate direct UTF-8 ingestion from legacy-detection workloads; label warm OS caches honestly, preserve raw samples, and report any non-equivalent bulk/deletion guarantees.
- [ ] Implement report validation and document isolated cold-cache methodology as optional; never drop global caches or introduce a daemon to improve timing.
- [ ] Verify deterministic metric calculations and a complete small paired run.
- [ ] Commit as “perf: measure comparable vault and filesystem workloads”.

### 21. Record scale evidence and honest agent coverage

**Files:** Create `benchmarks/results/v1-small.json`, `benchmarks/results/v1-scale.json`, and `benchmarks/results/agent-trials.json`; modify `benchmarks/run.py`, `tests/test_benchmarks.py`, `benchmarks/README.md`, and `benchmarks/agent-trials.md`.

**Interfaces:** Consumes `run_profile` and `validate_report`; produces actual small/scale evidence and agent-trial records with harness/model versions, scenario, status, tool calls, mistakes, and completion outcome. Untested records state the reason and use null for unavailable measurements.

- [ ] Add report-validation cases expecting rejection of missing operations, undocumented cold-cache labels, absent raw samples, and undersized scale corpora.
- [ ] Run these cases; confirm the validator currently accepts an invalid fixture instead of rejecting it.
- [ ] Extend the validator for the new conditions; verify valid fixtures pass and invalid fixtures fail validation.
- [ ] Run the full correctness suite before expensive measurements.
- [ ] Start the small and scale profiles into their named result files; inspect resource usage and final-state checks without changing durability to improve scores.
- [ ] Validate both actual reports, calculate storage overhead from input bytes, and document measured wins, losses, and limitations without a blanket parity claim.
- [ ] Run live agent scenarios only with explicitly available installations/credentials; otherwise write untested records. Keep live-agent latency separate from storage timings.
- [ ] Verify report consistency and all accumulated tests.
- [ ] Commit as “perf: record v1 scale and agent evaluation”.

## Self-review

- Spec sections 1–4 map to Tasks 1–4 and 13; sections 5–6 map to Tasks 5–16; section 7 maps to Tasks 17 and 21; sections 8–10 map to all test cycles and Tasks 18–21. Section 11 informs Task 17's harness documentation checks.
- Public names and record fields are defined in their producing task and reused unchanged; import uses caller-owned transaction helpers, not nested public write transactions.
- No search, renamed POSIX commands, native-tool adapters, daemon, or history storage is introduced. No implementation code or execution-handoff offer is included.
- Format and placeholder checks confirm exact constraint quotations, fully qualified task paths, separate verification/commit steps, and no code blocks or incomplete instructions.
