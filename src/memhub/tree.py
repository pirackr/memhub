"""Directory-tree enforcement and component resolution.

Two functions compose a normalized virtual path with a validated SQLite
connection:

* :func:`resolve` walks the tree from the root and returns the :class:`sqlite3.Row`
  for the requested path, raising :class:`Missing` when the path does not exist
  (or cannot be traversed because an intermediate component is not a directory).
* :func:`ensure_directories` walks the same path, creating any missing
  components as directories on the way, and returns the :class:`sqlite3.Row` for
  the final component.

The tree itself is enforced entirely by the schema: a single root, a unique
sibling index, foreign keys, and row triggers (see ``schema.sql``). These
functions neither start nor commit a transaction; the caller decides when a
batch of inserts becomes durable, which lets :func:`ensure_directories` be
rolled back atomically.
"""

from __future__ import annotations

import sqlite3

from .errors import Conflict, Missing

__all__ = ["resolve", "ensure_directories"]

# Entry kinds. Files store document content; directories never do.
FILE = "file"
DIRECTORY = "directory"


def _root_row(connection: sqlite3.Connection) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM entries WHERE parent_id IS NULL"
    ).fetchone()
    if row is None:
        raise Missing(
            "no_root",
            "vault has no root entry; this is only possible with a corrupt schema",
        )
    return row


def _components(path: str) -> list[str]:
    """Split a normalized absolute path into its non-empty components.

    ``"/"`` yields ``[]``; ``"/a/b"`` yields ``["a", "b"]``. The input is
    assumed already normalized by :func:`memhub.paths.normalize_path`, so no
    empty components and no ``.``/``..`` navigation are possible.
    """
    stripped = path.strip("/")
    if not stripped:
        return []
    return stripped.split("/")


def _fetch_child(connection: sqlite3.Connection, parent_id, name: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM entries WHERE parent_id = ? AND name = ?",
        (parent_id, name),
    ).fetchone()


def resolve(connection: sqlite3.Connection, path: str) -> sqlite3.Row:
    """Return the row at ``path``, resolving every component from the root.

    ``"/"`` returns the root. Any intermediate component that is not a
    directory makes the path unresolvable and raises :class:`Conflict`. A path
    whose final component is missing raises :class:`Missing`.
    """
    components = _components(path)
    if not components:
        return _root_row(connection)

    parent_id = _root_row(connection)["id"]
    for index, component in enumerate(components):
        is_last = index == len(components) - 1
        row = _fetch_child(connection, parent_id, component)
        if row is None:
            raise Missing(
                "no_such_path",
                f"path component {component!r} does not exist under the current parent",
            )
        if not is_last and row["kind"] != DIRECTORY:
            raise Conflict(
                "path_component_not_directory",
                f"path component {component!r} is a {row['kind']}, not a directory",
            )
        parent_id = row["id"]

    return connection.execute(
        "SELECT * FROM entries WHERE id = ?",
        (parent_id,),
    ).fetchone()


def ensure_directories(connection: sqlite3.Connection, path: str) -> sqlite3.Row:
    """Create every missing component of ``path`` as a directory.

    Returns the row for the final component, creating missing ancestors on the
    way and reusing existing directories. Descending through an existing
    non-directory is a conflict. All inserts happen on ``connection``; the
    caller owns transaction boundaries so a failed run rolls back cleanly.
    """
    components = _components(path)
    if not components:
        return _root_row(connection)

    parent_id = _root_row(connection)["id"]
    last_id = None
    for component in components:
        row = _fetch_child(connection, parent_id, component)
        if row is None:
            stamp = _utcnow_iso()
            connection.execute(
                "INSERT INTO entries "
                "(parent_id, name, kind, content, created_at, updated_at) "
                "VALUES (?, ?, 'directory', NULL, ?, ?)",
                (parent_id, component, stamp, stamp),
            )
            last_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        elif row["kind"] != DIRECTORY:
            raise Conflict(
                "path_component_not_directory",
                f"path component {component!r} is a {row['kind']}, not a directory",
            )
        else:
            last_id = row["id"]
        parent_id = last_id

    return connection.execute(
        "SELECT * FROM entries WHERE id = ?",
        (last_id,),
    ).fetchone()


def _utcnow_iso() -> str:
    """UTC ISO-8601 timestamp for freshly created entries.

    Imported lazily so package load stays cheap; timestamps are only written by
    :func:`ensure_directories`.
    """
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
