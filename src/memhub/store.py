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
