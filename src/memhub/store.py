"""SQLite-backed storage for memhub."""

import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import ContextManager, Iterator

_SCHEMA = """\
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
"""

_SCHEMA_VERSION = 1


class MemhubError(Exception):
    """Base error for memhub storage operations."""


def normalize_path(p: str) -> str:
    """Normalize and validate a memory path.

    The result always starts with '/' and has no trailing slash, except the
    root '/' itself. Paths are case-sensitive; components may contain any
    character except '/'.

    Raises MemhubError if p is empty, contains '.' or '..' components, or
    contains empty components (double slashes, including a trailing '//').
    """
    if not p:
        raise MemhubError("path must not be empty")
    if "//" in p:
        raise MemhubError(f"invalid path {p!r}: empty component")
    if p != "/" and p.endswith("/"):
        p = p[:-1]
    body = p[1:] if p.startswith("/") else p
    if body == "":
        return "/"
    components = body.split("/")
    for c in components:
        if c in (".", ".."):
            raise MemhubError(f"invalid path {p!r}: '.'/'..' component not allowed")
    return "/" + "/".join(components)


def _now() -> str:
    """Current UTC time as an ISO-8601 string, second precision, 'Z' suffix."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Memory:
    """A stored memory record (metadata only, no content)."""

    id: str
    path: str
    content_type: str
    size: int
    created_at: str
    updated_at: str


_COLUMNS = "id, path, content_type, size, created_at, updated_at"


def _row_to_memory(row: tuple) -> Memory:
    return Memory(
        id=row[0],
        path=row[1],
        content_type=row[2],
        size=row[3],
        created_at=row[4],
        updated_at=row[5],
    )


def content_type_for(data: bytes) -> str:
    """Sniff content type: 'text/plain' if data is valid UTF-8, else binary."""
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return "application/octet-stream"
    return "text/plain"


def is_memory(conn: sqlite3.Connection, path: str) -> bool:
    """True if an exact memory exists at path."""
    p = normalize_path(path)
    return conn.execute(
        "SELECT 1 FROM memories WHERE path = ? LIMIT 1", (p,)
    ).fetchone() is not None


def has_descendants(conn: sqlite3.Connection, path: str) -> bool:
    """True if any stored memory lives below path.

    A path p is an ancestor of m when m starts with p + '/'; the root '/' is an
    ancestor of every memory.
    """
    p = normalize_path(path)
    prefix = "/" if p == "/" else p + "/"
    return conn.execute(
        "SELECT 1 FROM memories WHERE substr(path, 1, ?) = ? LIMIT 1",
        (len(prefix), prefix),
    ).fetchone() is not None


def _proper_ancestors(p: str) -> list[str]:
    """Ancestor directories of a normalized non-root path, outermost first.

    '/a/b/c' -> ['/a', '/a/b']. The root '/' is omitted: it can never hold a
    memory (see put).
    """
    parts = p.split("/")  # '/a/b' -> ['', 'a', 'b']
    return ["/".join(parts[:i]) for i in range(2, len(parts))]


def put(
    conn: sqlite3.Connection,
    path: str,
    data: bytes,
    content_type: str | None = None,
) -> Memory:
    """Insert a new memory, or overwrite the one at path.

    Overwriting keeps the existing id and created_at and updates content,
    content_type, size and updated_at; a fresh insert gets a new uuid4 id.
    Text ('text/plain') is stored decoded in the content column, anything else
    as bytes in the blob column.

    Raises MemhubError if path is the root, if an ancestor of path is an
    existing memory, if path is an ancestor of existing memories, or if
    content_type is 'text/plain' but data is not valid UTF-8. The caller owns
    the transaction (open_store commits on clean exit).
    """
    p = normalize_path(path)
    if p == "/":
        raise MemhubError("cannot write a memory at root '/'")
    for ancestor in _proper_ancestors(p):
        if is_memory(conn, ancestor):
            raise MemhubError(
                f"cannot write {p!r}: ancestor {ancestor!r} is a memory"
            )
    if has_descendants(conn, p):
        raise MemhubError(
            f"cannot write {p!r}: it is a directory (memories exist below it)"
        )

    ctype = content_type if content_type is not None else content_type_for(data)
    if ctype == "text/plain":
        try:
            text: str | None = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MemhubError(
                f"cannot store {p!r} as text/plain: data is not valid UTF-8"
            ) from exc
        blob: bytes | None = None
    else:
        text = None
        blob = data

    now = _now()
    existing = conn.execute(
        "SELECT id FROM memories WHERE path = ?", (p,)
    ).fetchone()
    if existing is not None:
        memory_id = existing[0]
        conn.execute(
            "UPDATE memories SET content = ?, blob = ?, content_type = ?, "
            "size = ?, updated_at = ? WHERE id = ?",
            (text, blob, ctype, len(data), now, memory_id),
        )
    else:
        memory_id = uuid.uuid4().hex
        created_at = now
        conn.execute(
            "INSERT INTO memories (id, path, content, blob, content_type, size, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (memory_id, p, text, blob, ctype, len(data), created_at, now),
        )
    return stat(conn, p)


def read_content(conn: sqlite3.Connection, path: str) -> bytes:
    """Return the memory's content byte-exact.

    Raises MemhubError if no memory exists at path.
    """
    p = normalize_path(path)
    row = conn.execute(
        "SELECT content, blob FROM memories WHERE path = ?", (p,)
    ).fetchone()
    if row is None:
        raise MemhubError(f"no memory at {p!r}")
    text, blob = row
    if text is not None:
        return text.encode("utf-8")
    return bytes(blob)


def stat(conn: sqlite3.Connection, path: str) -> Memory:
    """Return the metadata of the memory at path.

    Raises MemhubError if no memory exists at path.
    """
    p = normalize_path(path)
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM memories WHERE path = ?", (p,)
    ).fetchone()
    if row is None:
        raise MemhubError(f"no memory at {p!r}")
    return _row_to_memory(row)


@dataclass(frozen=True)
class Entry:
    """An ls result: a memory, or an implicit directory (memory is None)."""

    path: str
    is_dir: bool
    memory: Memory | None = None


def _descendant_rows(conn: sqlite3.Connection, prefix: str) -> list[tuple]:
    """All memory rows under prefix (a normalized path plus trailing '/')."""
    return conn.execute(
        f"SELECT {_COLUMNS} FROM memories WHERE substr(path, 1, ?) = ? "
        "ORDER BY path",
        (len(prefix), prefix),
    ).fetchall()


def ls(conn: sqlite3.Connection, path: str, recursive: bool = False) -> list[Entry]:
    """List entries at path, sorted by path.

    ls on a memory path returns that single entry. On a directory (the root,
    or any path with descendants), non-recursive lists immediate children --
    memories plus implicit directories -- and recursive lists all descendant
    memories. An empty directory (the root of an empty store) yields [].

    Raises MemhubError if path is neither a memory nor a directory (no
    memory, no descendants).
    """
    p = normalize_path(path)
    if is_memory(conn, p):
        return [Entry(p, False, stat(conn, p))]
    if p != "/" and not has_descendants(conn, p):
        raise MemhubError(f"no memory or directory at {p!r}")
    prefix = "/" if p == "/" else p + "/"
    rows = _descendant_rows(conn, prefix)
    if recursive:
        return [Entry(r[1], False, _row_to_memory(r)) for r in rows]
    entries: dict[str, Entry] = {}
    for row in rows:
        rest = row[1][len(prefix):]
        head = rest.split("/")[0]
        child = prefix + head
        if rest == head:
            entries[child] = Entry(child, False, _row_to_memory(row))
        else:
            entries.setdefault(child, Entry(child, True, None))
    return sorted(entries.values(), key=lambda e: e.path)


def rm(conn: sqlite3.Connection, path: str, recursive: bool = False) -> int:
    """Delete the memory at path, returning 1.

    With recursive=True, delete the whole subtree below a directory path and
    return the number of memories removed. Without recursive, a directory with
    descendants raises, as does a missing path. The root '/' is a directory,
    never a memory, and can never be removed.

    The caller owns the transaction (open_store commits on clean exit).
    """
    p = normalize_path(path)
    if p == "/":
        raise MemhubError("cannot remove root '/'")
    if is_memory(conn, p):
        conn.execute("DELETE FROM memories WHERE path = ?", (p,))
        return 1
    if has_descendants(conn, p):
        if not recursive:
            raise MemhubError(
                f"{p!r} is a directory with descendants; use recursive=True"
            )
        prefix = p + "/"
        cur = conn.execute(
            "DELETE FROM memories WHERE substr(path, 1, ?) = ?",
            (len(prefix), prefix),
        )
        return cur.rowcount
    raise MemhubError(f"no memory at {p!r}")


def mv(conn: sqlite3.Connection, src: str, dst: str) -> Memory:
    """Move the memory at src to dst, returning its updated metadata.

    The moved record keeps its id and created_at; path and updated_at are
    refreshed. Moving a memory onto its own path is a no-op. If dst already holds a memory it is replaced: dst's old row is
    deleted and the moved record takes its place, keeping the moved record's
    id. Collision rules match put: an ancestor of dst must not be a memory,
    and dst must not be an ancestor of other memories (src itself excluded).
    The root '/' can never be moved to or from.

    Raises MemhubError if src is missing or is not a memory, or on any
    collision. The caller owns the transaction (open_store commits on clean
    exit).
    """
    s = normalize_path(src)
    d = normalize_path(dst)
    if s == "/" or d == "/":
        raise MemhubError("cannot move to or from root '/'")
    if not is_memory(conn, s):
        raise MemhubError(f"no memory at {s!r}")
    if s == d:
        return stat(conn, d)  # no-op move
    for ancestor in _proper_ancestors(d):
        if is_memory(conn, ancestor):
            raise MemhubError(
                f"cannot move to {d!r}: ancestor {ancestor!r} is a memory"
            )
    prefix = d + "/"
    conflict = conn.execute(
        "SELECT path FROM memories WHERE substr(path, 1, ?) = ? AND path != ? "
        "LIMIT 1",
        (len(prefix), prefix, s),
    ).fetchone()
    if conflict is not None:
        raise MemhubError(
            f"cannot move to {d!r}: it is a directory (memories exist below it)"
        )
    now = _now()
    conn.execute("DELETE FROM memories WHERE path = ?", (d,))
    conn.execute(
        "UPDATE memories SET path = ?, updated_at = ? WHERE path = ?",
        (d, now, s),
    )
    return stat(conn, d)


def init_store(db_path: Path) -> None:
    """Create the SQLite file and schema (idempotent)."""
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_SCHEMA)
        conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        conn.commit()
    finally:
        conn.close()


def open_store(db_path: Path) -> ContextManager[sqlite3.Connection]:
    """Open the store with WAL enabled.

    On clean exit, checkpoints the WAL (TRUNCATE) and closes the connection,
    leaving no -wal/-shm sidecar files behind, so the .sqlite file is safe
    to copy.
    """
    return _open_store(db_path)


@contextmanager
def _open_store(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        yield conn
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
