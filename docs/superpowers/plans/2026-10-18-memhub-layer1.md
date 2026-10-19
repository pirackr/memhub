# memhub Layer 1 (raw store) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Python CLI (`memhub`) that stores raw memories as path-addressed files inside a SQLite database, with filesystem-style commands (`cp`, `cat`, `ls`, `rm`, `mv`, `stat`).

**Architecture:** Three modules: `store.py` (SQLite schema + record operations), `config.py` (which store file is active), `cli.py` (argparse front door, JSON output). One SQLite file per store/context; every command opens a short-lived connection and checkpoints the WAL on exit so the file is always safe to `cp`.

**Tech Stack:** Python 3.14, stdlib `sqlite3` + `argparse` + `tomllib`/`tomli-w`, `uv` for packaging, `pytest` for tests. No runtime dependencies beyond the stdlib (tomli-w is the one allowed exception, for writing config).

**Spec:** `docs/superpowers/specs/2026-10-18-memhub-design.md`

## Global Constraints

- Language: Python; storage: SQLite via stdlib `sqlite3`; no servers, daemons, or network API.
- v1 stores no history: overwrite replaces content in place.
- Content is stored verbatim (text or bytes); layer 1 never interprets it.
- No policy/enforcement between stores; merging is `cp` of the file by the user.
- CLI uses real filesystem command names as primary commands.
- Timestamps are ISO 8601 UTC strings.

## Review Focus

These are spec-implied behaviors no single happy-path test covers; each line gets a pinning test in the named task:

- **`cp` safety:** after any command exits, no `-wal`/`-shm` sidecar files remain next to the database — a plain `cp` of the file right after a write must contain that write (Task 1).
- **Binary fidelity:** arbitrary bytes (invalid UTF-8, NULs) round-trip byte-exact through `cp`/`cat` (Task 3).
- **File/dir collision:** a path cannot be both a memory and a directory prefix for other memories; both directions of the collision are rejected (Task 3).
- **Identity stability:** overwrite keeps the record id; delete + re-create yields a new id (Task 3).
- **Clean errors:** missing paths and invalid paths produce a one-line error on stderr and exit code 1 — never a traceback (Task 6).

---

### Task 1: Project scaffolding + store open/init

**Files:**
- Create: `pyproject.toml`, `src/memhub/__init__.py`, `src/memhub/store.py`, `tests/test_store.py`

**Interfaces:**
- Produces:
  - `store.MemhubError(Exception)`
  - `store.init_store(db_path: Path) -> None` — creates the SQLite file and schema (idempotent).
  - `store.open_store(db_path: Path) -> ContextManager[sqlite3.Connection]` — context manager; enables WAL; on exit runs `PRAGMA wal_checkpoint(TRUNCATE)` and closes, leaving no `-wal`/`-shm` files.

Schema (applied by `init_store`, version tracked with `PRAGMA user_version = 1`):

```sql
CREATE TABLE IF NOT EXISTS memories (
  id TEXT NOT NULL PRIMARY KEY,          -- uuid4 hex, immutable
  path TEXT NOT NULL UNIQUE,             -- normalized absolute path
  content TEXT,                          -- text content (NULL for binary)
  blob BLOB,                             -- binary content (NULL for text)
  content_type TEXT NOT NULL,
  size INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

- [ ] **Step 1: Scaffold with uv**

```bash
uv init --package --name memhub --python 3.14 .   # adjust if uv init complains about existing files
uv add --dev pytest
```

`pyproject.toml` gets `[project] dependencies = []` plus `tomli-w` as a runtime dep (needed in Task 5). Keep the default `src/memhub` layout.

- [ ] **Step 2: Write the failing tests in `tests/test_store.py`**

```python
def test_init_store_creates_db(tmp_path):
    db = tmp_path / "s.sqlite"
    init_store(db)
    assert db.exists()

def test_init_store_is_idempotent(tmp_path): ...  # calling twice does not raise

def test_open_store_checkpoints_wal_on_exit(tmp_path):
    db = tmp_path / "s.sqlite"
    init_store(db)
    with open_store(db) as conn:
        conn.execute("PRAGMA journal_mode")  # touch the store
    assert not db.with_name(db.name + "-wal").exists()
    assert not db.with_name(db.name + "-shm").exists()
```

- [ ] **Step 3: Run to verify failure** — `uv run pytest tests/test_store.py -v` → ImportError/FAIL.
- [ ] **Step 4: Implement `init_store` and `open_store`** in `src/memhub/store.py`. `open_store` sets `journal_mode=WAL` on open; on clean exit executes `wal_checkpoint(TRUNCATE)` before `close()`.
- [ ] **Step 5: Run tests, verify pass** — `uv run pytest tests/test_store.py -v`.
- [ ] **Step 6: Commit** — `git add -A && git commit -m "feat: scaffold memhub package with sqlite store open/init"`

---

### Task 2: Path normalization and validation

**Files:**
- Modify: `src/memhub/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Produces: `store.normalize_path(p: str) -> str` — raises `MemhubError` on invalid input.

Rules (from spec): result always starts with `/`, no trailing slash (except root `/` itself), case-sensitive, components may contain any character except `/` (spaces, unicode fine), rejects: empty string, `.`/`..` components, empty components (double slashes).

- [ ] **Step 1: Write failing tests** — cover: `"a/b"` → `"/a/b"`; `"/a/b/"` → `"/a/b"`; `"/"` → `"/"`; `"a//b"`, `"a/../b"`, `"a/./b"`, `""` all raise `MemhubError`; unicode/spaces pass through: `"/notes/été 2026.md"` unchanged.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement `normalize_path`.**
- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit** — `git commit -am "feat: path normalization and validation"`

---

### Task 3: Record operations — put / read / stat

**Files:**
- Modify: `src/memhub/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: `normalize_path`, `open_store` (Tasks 1–2).
- Produces:
  - `store.Memory` dataclass: `id: str, path: str, content_type: str, size: int, created_at: str, updated_at: str`
  - `store.content_type_for(data: bytes) -> str` — `"text/plain"` if data decodes as UTF-8, else `"application/octet-stream"`.
  - `store.put(conn, path: str, data: bytes, content_type: str | None = None) -> Memory` — insert or overwrite; stores text in `content` (decoded) when type is `text/plain`, else in `blob`.
  - `store.read_content(conn, path: str) -> bytes` — byte-exact.
  - `store.stat(conn, path: str) -> Memory`
  - `store.is_memory(conn, path) -> bool` and `store.has_descendants(conn, path) -> bool` (used by Tasks 4 and 6).

Collision rules (Review Focus): `put` rejects when any ancestor of `path` is an existing memory; rejects when `path` is an ancestor (prefix directory) of existing memories; overwriting the same path is allowed and **keeps `id` and `created_at`**, updating `content`, `size`, `content_type`, `updated_at`. A fresh insert gets a new uuid4 id.

- [ ] **Step 1: Write failing tests** — cover: text round-trip; **binary fidelity** with `b"\x00\xff\xfe"`; `put` twice at same path overwrites and keeps `id` (Review Focus); delete row + re-put yields different `id`; ancestor collision (`put /a` after `put /a/b` raises, and vice versa); `stat` on missing path raises `MemhubError`; `content_type_for` sniffing and `--content-type`-style override honored.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** the five functions plus `Memory`. Ancestor check: a path `p` is an ancestor of `m` iff `m.path.startswith(p + "/")` (root `/` is ancestor of everything).
- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit** — `git commit -am "feat: record put/read/stat with collision rules"`

---

### Task 4: Directory operations — ls / rm / mv

**Files:**
- Modify: `src/memhub/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: Task 3 helpers (`is_memory`, `has_descendants`).
- Produces:
  - `store.Entry` dataclass: `path: str, is_dir: bool, memory: Memory | None` (`memory` is `None` for implicit directories)
  - `store.ls(conn, path: str, recursive: bool = False) -> list[Entry]` — `ls` on `/` lists top-level entries; non-recursive lists immediate children (memories + implicit dirs); recursive lists all descendant memories. `ls` on a memory path returns that single entry. Empty directory → `[]`. Missing path (no memory, no descendants) raises `MemhubError`.
  - `store.rm(conn, path: str, recursive: bool = False) -> int` — deletes one memory; with `recursive=True` deletes the subtree, returning count. Non-recursive `rm` on a directory with descendants raises `MemhubError`; missing path raises.
  - `store.mv(conn, src: str, dst: str) -> Memory` — moves a memory; same collision rules as `put` (dst must not be a memory ancestor/descendant conflict; overwriting dst memory allowed); source must exist.

- [ ] **Step 1: Write failing tests** — seed `/a.md`, `/notes/one.md`, `/notes/deep/two.md`, then: `ls /` shows `a.md` and dir `notes`; `ls /notes` shows `one.md` + dir `deep`; `ls /notes -R` shows both memories; `ls /empty` raises; `rm /notes` raises without recursive, with recursive removes 3 and returns 3; `mv /a.md /b.md` moves content, keeps id; `mv` onto a descendant (`/notes` → `/notes/x`) raises.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** `ls`, `rm`, `mv`. Derive immediate children by stripping the prefix and taking the first component of what remains.
- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit** — `git commit -am "feat: ls/rm/mv directory operations"`

---

### Task 5: Store config and resolution

**Files:**
- Create: `src/memhub/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces:
  - `config.config_path() -> Path` — `~/.memhub/config.toml`, overridable via `MEMHUB_HOME` env var (test hook).
  - `config.resolve_store() -> Path` — `MEMHUB_DB` env var (a file path) wins; else `default_store` name from config → its registered path; raises `MemhubError` if neither exists ("no store configured; run memhub init").
  - `config.register_store(name: str, db_path: Path, set_default: bool) -> None` — creates/updates the config file atomically.

Config format:

```toml
default_store = "personal"

[stores]
personal = "/home/u/.memhub/personal.sqlite"
work = "/srv/work.sqlite"
```

- [ ] **Step 1: Write failing tests** (use `tmp_path` + `monkeypatch` for env/home): no config + no env → `MemhubError`; `MEMHUB_DB` wins over config; default store resolves; `register_store` creates file, second call adds a store, `set_default=True` flips `default_store`; file round-trips through `tomllib`/`tomli-w`.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement `config.py`.**
- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit** — `git commit -am "feat: store config resolution via config file and env"`

---

### Task 6: CLI wiring + README

**Files:**
- Create: `src/memhub/cli.py`, `tests/test_cli.py`, `README.md`
- Modify: `pyproject.toml` (console script `memhub = "memhub.cli:main"`)

**Interfaces:**
- Consumes: everything from Tasks 1–5.
- Produces: `cli.main(argv: list[str] | None = None) -> int`.

Commands (store paths are absolute — start with `/`; anything else is a local file; `-` is stdin):

| Command | Behavior |
|---|---|
| `memhub init [NAME] [--path PATH]` | create store (default name `default`, default path `~/.memhub/<name>.sqlite`), register in config, set default if first store. Prints JSON `{"store": name, "path": ...}`. |
| `memhub cp SRC DEST` | SRC = local file, `-` (stdin), or store path (store→store copy). DEST = store path; trailing `/` means into that directory using SRC's basename (rejected for `-`). Overwrites allowed. Prints JSON of resulting `Memory`. |
| `memhub cat PATH` | raw bytes to stdout, byte-exact. |
| `memhub ls [PATH] [-R]` | PATH defaults to `/`. JSON array of entries `{path, is_dir, content_type, size, created_at, updated_at}` (nulls for dirs). |
| `memhub rm PATH [-r]` | JSON `{"removed": n}`. |
| `memhub mv SRC DEST` | JSON of moved `Memory`. |
| `memhub stat PATH` | JSON of `Memory`. |
| `memhub mkdir PATH` | validates path, succeeds as no-op (directories are implicit). |

Errors: `MemhubError` → message on stderr, exit 1, no traceback (Review Focus). Unknown command → argparse usage, exit 2.

- [ ] **Step 1: Write failing end-to-end tests in `tests/test_cli.py`** — call `main([...])` with `MEMHUB_HOME`/`MEMHUB_DB` monkeypatched to `tmp_path`: full flow init → cp (file, stdin, trailing-slash dest) → cat byte-exact → ls → stat → mv → rm -r; **clean-error test**: `cat` on missing path exits 1 with stderr message and no "Traceback" in output; `cat` exit code 0 with exact bytes on stdout.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement `cli.py`** with argparse subparsers; each subcommand resolves the store via `config.resolve_store()`, opens with `open_store`, calls the store function, prints JSON (`json.dumps(..., ensure_ascii=False)`). Register console script in `pyproject.toml`.
- [ ] **Step 4: Run all tests, verify pass** — `uv run pytest -v`.
- [ ] **Step 5: Smoke-test the installed script** — `uv run memhub init smoke --path /tmp/smoke.sqlite && echo hello | uv run memhub cp - /greeting.txt && uv run memhub cat /greeting.txt` prints `hello`; then `cp /tmp/smoke.sqlite /tmp/smoke2.sqlite` and `cat` from the copy shows the memory (the partner's merge workflow works). Remove `/tmp/smoke*` and any `~/.memhub` side effects from the smoke test if it touched real config (use `MEMHUB_HOME=/tmp/...`).
- [ ] **Step 6: Write `README.md`** — what it is, install, one worked example (init, cp, cat, ls), store config, the `cp`-the-file merge workflow, and the layer-1 "raw store, higher layers later" framing.
- [ ] **Step 7: Commit** — `git add -A && git commit -m "feat: memhub CLI with fs-style commands"`
