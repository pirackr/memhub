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
from typing import Optional, Tuple

from .errors import Conflict, InvalidInput, Missing
from .models import Entry, ListResult
from .paths import normalize_path

__all__ = ["resolve", "ensure_directories", "list_entries", "remove_entry"]

# Entry kinds. Files store document content; directories never do.
FILE = "file"
DIRECTORY = "directory"


def _entry_from_row(row: sqlite3.Row, path: str) -> Entry:
    """Build an :class:`Entry` from a resolved/computed row and its canonical path.

    Size is the UTF-8 byte count of a file's content, or ``None`` for a
    directory, mirroring the storage library's entry view.
    """
    kind = row["kind"]
    size_bytes: Optional[int] = None
    if kind == FILE and row["content"] is not None:
        size_bytes = len(row["content"].encode("utf-8"))
    return Entry(
        id=row["id"],
        path=path,
        kind=kind,
        size_bytes=size_bytes,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _validate_pagination(limit: int, offset: int) -> None:
    """Validate the paging arguments of :func:`list_entries`.

    ``limit`` must be a positive integer and ``offset`` a nonnegative integer;
    ``bool`` is rejected even though it subclasses ``int``. Raises
    :class:`InvalidInput` otherwise; the arguments are returned unchanged on
    success.
    """
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise InvalidInput(
            "invalid_limit",
            "limit must be an integer",
        )
    if limit < 1:
        raise InvalidInput(
            "invalid_limit",
            f"limit must be at least 1, got {limit}",
        )
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise InvalidInput(
            "invalid_offset",
            "offset must be an integer",
        )
    if offset < 0:
        raise InvalidInput(
            "invalid_offset",
            f"offset must be nonnegative, got {offset}",
        )


def _snapshot_rows(
    connection: sqlite3.Connection,
    parent_row: sqlite3.Row,
    recursive: bool,
    take: int,
    offset: int,
) -> list[sqlite3.Row]:
    """Fetch up to ``take`` rows for one page using the parent/name index.

    The immediate listing orders siblings by ``name``; the ``entries_sibling_unique``
    index on ``(parent_id, name)`` serves that lookup. The recursive listing
    descends through the same index (a join on ``parent_id``) while building
    each descendant's full virtual path, then orders by that path. In both
    cases the extra ``take = limit + 1`` row is a one-entry look-ahead: it
    answers whether a following page exists without a second query.
    """
    parent_id = parent_row["id"]
    if not recursive:
        return connection.execute(
            "SELECT * FROM entries WHERE parent_id = ? ORDER BY name LIMIT ? OFFSET ?",
            (parent_id, take, offset),
        ).fetchall()
    return connection.execute(
        "WITH RECURSIVE subtree AS ("
        "  SELECT id, parent_id, name, kind, content, created_at, updated_at, "
        "         CAST('/' || name AS TEXT) AS vpath "
        "  FROM entries WHERE parent_id = ? "
        "  UNION ALL "
        "  SELECT e.id, e.parent_id, e.name, e.kind, e.content, e.created_at, e.updated_at, "
        "         sp.vpath || '/' || e.name "
        "  FROM entries e JOIN subtree sp ON e.parent_id = sp.id"
        ") SELECT * FROM subtree ORDER BY vpath LIMIT ? OFFSET ?",
        (parent_id, take, offset),
    ).fetchall()


def _materialize(
    rows: list[sqlite3.Row],
    parent_canonical: str,
    recursive: bool,
    limit: int,
    offset: int,
) -> Tuple[list[Entry], bool, Optional[int]]:
    """Build :class:`Entry` views and paging metadata from fetched rows.

    Only ``limit`` entries are returned; the extra look-ahead row (if any) is
    discarded. ``has_more`` is true when a look-ahead row existed, and
    ``next_offset`` is the following zero-based offset, or ``None`` on the
    final page.
    """
    entries: list[Entry] = []
    for row in rows[:limit]:
        if recursive:
            entry_path = row["vpath"]
        elif parent_canonical == "/":
            entry_path = "/" + row["name"]
        else:
            entry_path = parent_canonical + "/" + row["name"]
        entries.append(_entry_from_row(row, entry_path))
    has_more = len(rows) > limit
    next_offset = offset + limit if has_more else None
    return entries, has_more, next_offset


def list_entries(
    vault: "Vault",
    path: str = "/",
    recursive: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> ListResult:
    """List a directory's children (or descendants) with bounded pagination.

    ``path`` names the directory to list and defaults to the root. The listed
    directory itself is never included in ``entries``; each returned
    :class:`Entry` is a single child (immediate listing) or descendant
    (recursive listing) with its computed full virtual path, so directory
    contents are never expanded inside a parent's row.

    ``recursive`` includes every descendant rather than only the direct
    children. Entries are deterministically ordered by virtual path using
    case-sensitive binary ordering. ``limit`` is a positive page size (default
    100) and ``offset`` a zero-based skip (default 0).

    The listing runs inside a single snapshot (``BEGIN``) transaction so the
    children and their sizes are seen together. A missing path raises
    :class:`Missing`; listing a path that is a file raises :class:`Conflict`.
    A ``limit`` below 1 or a negative ``offset`` (or a non-integer limit/offset)
    raises :class:`InvalidInput`.

    On success it returns a :class:`ListResult`. ``has_more`` reports whether
    further entries remain past this page and ``next_offset`` is the following
    offset, or ``None`` on the final page.
    """
    _validate_pagination(limit, offset)
    canonical = normalize_path(path)

    with vault.transaction(write=False):
        parent = resolve(vault.connection, canonical)
        if parent["kind"] != DIRECTORY:
            raise Conflict(
                "not_a_directory",
                f"path {canonical!r} is a {parent['kind']}, not a directory to list",
            )
        rows = _snapshot_rows(
            vault.connection, parent, recursive, limit + 1, offset
        )
        entries, has_more, next_offset = _materialize(
            rows, canonical, recursive, limit, offset
        )

    return ListResult(entries=entries, has_more=has_more, next_offset=next_offset)


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


def _descendant_ids_bottom_up(
    connection: sqlite3.Connection, target_id: int
) -> list[int]:
    """Return ``target_id`` and every descendant, deepest row first.

    The whole subtree is gathered with a single recursive CTE, so no Python
    recursion is involved and neither the depth of the tree nor any cascade
    trigger's execution depth can be exhausted. Rows are ordered by ``depth``
    descending (ties by ``id``) so a parent is always deleted *after* its
    children. With foreign keys enabled that bottom-up order is exactly what
    makes each ``DELETE`` succeed on its own: a parent row only disappears once
    every row that points at it has already gone.
    """
    return [
        row[0]
        for row in connection.execute(
            "WITH RECURSIVE subtree(id, depth) AS ("
            "  SELECT id, 0 FROM entries WHERE id = ?"
            "  UNION ALL "
            "  SELECT e.id, sp.depth + 1 "
            "  FROM entries e JOIN subtree sp ON e.parent_id = sp.id"
            ") SELECT id FROM subtree ORDER BY depth DESC, id DESC",
            (target_id,),
        ).fetchall()
    ]


def _remove_in_transaction(
    connection: sqlite3.Connection, canonical: str, recursive: bool
) -> None:
    """Delete ``canonical`` (and, when requested, its subtree) in a write txn.

    The connection must already be inside a caller-owned write transaction; this
    helper never begins or commits one. The target is resolved first, so a
    missing path raises :class:`Missing` before anything is touched. The root is
    rejected explicitly with :class:`Conflict` (the schema trigger is the
    backstop for raw callers that bypass this helper). A non-empty directory is
    refused unless ``recursive`` is true -- this is the recursive guard.
    When recursive removal is allowed, descendants are collected with SQL and
    deleted bottom-up (see :func:`_descendant_ids_bottom_up`), so the foreign
    key chain never breaks and Python recursion depth is irrelevant.

    The single surviving direct parent is updated in the same transaction; its
    own ancestors are left untouched, matching the "directly affected directory
    only" timestamp rule.
    """
    row = resolve(connection, canonical)

    if row["parent_id"] is None:
        raise Conflict(
            "root_protected",
            "the root entry cannot be removed",
        )

    if row["kind"] == DIRECTORY:
        has_child = (
            connection.execute(
                "SELECT 1 FROM entries WHERE parent_id = ? LIMIT 1",
                (row["id"],),
            ).fetchone() is not None
        )
        if has_child and not recursive:
            raise Conflict(
                "directory_not_empty",
                f"path {canonical!r} is a non-empty directory; "
                "pass recursive=True to remove it and its subtree",
            )

    # ``_descendant_ids_bottom_up`` always includes the target itself (depth 0)
    # plus, when recursive, every reachable child; a file or empty directory
    # simply yields its own row. Deleting bottom-up keeps the FK chain valid.
    for target_id in _descendant_ids_bottom_up(connection, row["id"]):
        connection.execute(
            "DELETE FROM entries WHERE id = ?", (target_id,)
        )

    # Deletion changes the removed node's direct-parent membership, so only that
    # directory's updated_at advances; ancestors above it are not rewritten.
    connection.execute(
        "UPDATE entries SET updated_at = ? WHERE id = ?",
        (_utcnow_iso(), row["parent_id"]),
    )


def remove_entry(vault: "Vault", path: str, recursive: bool = False) -> None:
    """Remove a file or an empty directory, or an entire subtree when recursive.

    ``path`` is normalized before any resolution. The whole removal runs through
    the vault's writer transaction, so a missing path raises :class:`Missing`,
    an unauthorized non-empty-directory removal raises
    :class:`~memhub.errors.Conflict` (``directory_not_empty``), and root deletion
    raises :class:`~memhub.errors.Conflict` (``root_protected``). With
    ``recursive=True`` the target's descendants are collected with SQL and
    deleted bottom-up in the same transaction, and the surviving direct parent is
    updated too. A failure partway through -- injected or otherwise -- rolls the
    entire removal back, leaving surviving siblings and ancestor timestamps
    unchanged, and this function only reports success after the commit.
    """
    canonical = normalize_path(path)
    with vault.transaction(write=True):
        _remove_in_transaction(vault.connection, canonical, recursive)


def _utcnow_iso() -> str:
    """UTC ISO-8601 timestamp for freshly created entries.

    Imported lazily so package load stays cheap; timestamps are only written by
    :func:`ensure_directories`.
    """
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
