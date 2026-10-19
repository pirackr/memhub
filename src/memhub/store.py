"""SQLite-backed storage for memhub."""

import sqlite3
from contextlib import contextmanager
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
