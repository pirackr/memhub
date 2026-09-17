"""Atomic conditional document writes.

Two functions persist a decoded text document into the vault:

* :func:`write_file` is the public entry point. It normalizes the caller's path
  and runs the write through the vault's writer transaction, so the write
  reservation (``BEGIN IMMEDIATE``) is acquired *before* any condition is
  checked and any created parent directory is rolled back atomically when the
  write fails.
* :func:`write_in_transaction` is the transactional helper. It receives an
  already-normalized (canonical) path and a connection that is *already inside*
  a write transaction; it never begins or commits that transaction. Callers that
  own the transaction use this directly.

Both validate the admitted content (see :func:`memhub.text.validate_text`),
compute the document hash (see :func:`memhub.text.content_hash`), create missing
parent directories atomically, and honor the mutually exclusive
``if_match`` / ``if_absent`` conditions.

The storage library neither starts nor commits the surrounding transaction in
:func:`write_in_transaction`; the caller owns those boundaries. In
:func:`write_file` the :class:`~memhub.Vault` transaction owns them and rolls
back every created parent when the write raises.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Optional, Tuple

from .errors import Conflict, Missing
from .models import Entry, WriteResult
from .paths import normalize_path
from .text import content_hash, validate_text
from .tree import DIRECTORY, FILE, ensure_directories, resolve

if TYPE_CHECKING:
    from .vault import Vault

__all__ = [
    "Entry",
    "WriteResult",
    "write_file",
    "write_in_transaction",
]


def _parent_and_name(canonical: str) -> Tuple[str, str]:
    """Split a canonical path into its parent canonical path and leaf name.

    ``"/"`` is not a valid file path, so this helper is only called with paths
    that carry at least one component. ``"/a/b.txt"`` yields ``("/a", "b.txt")``;
    ``"/b.txt"`` yields ``("/", "b.txt")``.
    """
    stripped = canonical[1:]
    name = stripped.rsplit("/", 1)[-1]
    parent_part = stripped.rsplit("/", 1)[0] if "/" in stripped else ""
    parent = "/" + parent_part if parent_part else "/"
    return parent, name


def _entry_from_row(row: sqlite3.Row, path: str) -> Entry:
    """Build an :class:`Entry` from a resolved row and its canonical path."""
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


def _check_conditions(
    row: Optional[sqlite3.Row],
    digest: str,
    if_match: Optional[str],
    if_absent: bool,
) -> Optional[sqlite3.Row]:
    """Enforce the conditional-write rules for the resolved final component.

    ``row`` is the resolved final component (``None`` when the path is absent).
    Returns the existing file row when the write is an overwrite, otherwise
    ``None`` to signal that a fresh insert is required. Any condition that is not
    satisfied raises :class:`Conflict`.
    """
    if row is None:
        if if_match is not None:
            # A missing file cannot satisfy a required hash match.
            raise Conflict(
                "if_match_missing_document",
                "if_match requires an existing document to match",
            )
        return None

    if row["kind"] != FILE:
        raise Conflict(
            "exists_as_directory",
            f"path {row['name']!r} already exists as a directory, not a file",
        )

    existing_digest = content_hash(row["content"])
    if if_match is not None and if_match != existing_digest:
        raise Conflict(
            "stale_hash",
            "if_match hash does not match the current document content",
        )
    if if_absent:
        raise Conflict(
            "if_absent_exists",
            "if_absent requires the path to be absent, but it already exists",
        )

    return row


def write_in_transaction(
    connection: sqlite3.Connection,
    path: str,
    content: str,
    if_match: Optional[str] = None,
    if_absent: bool = False,
) -> WriteResult:
    """Persist ``content`` at the canonical ``path`` inside a write transaction.

    ``path`` must already be normalized by
    :func:`memhub.paths.normalize_path`. The connection must be inside an
    existing write transaction; this helper never issues ``BEGIN`` or
    ``COMMIT``. Parent directories missing from the tree are created, then the
    ``if_match`` / ``if_absent`` conditions are checked against the *current*
    state before the file is inserted or overwritten.

    Inserting a new file advances the directly affected parent directory's
    ``updated_at`` (its direct-child membership changed); overwriting an existing
    file updates only the file's own ``updated_at`` and leaves the parent
    untouched. ``created_at`` is preserved across overwrites.

    A successful write returns a :class:`WriteResult`. Unwritable content raises
    :class:`~memhub.errors.InvalidInput`; every conditional failure raises
    :class:`Conflict`.
    """
    if if_match is not None and if_absent:
        raise Conflict(
            "write_conditions_mutually_exclusive",
            "if_match and if_absent are mutually exclusive",
        )

    validate_text(content)
    digest = content_hash(content)

    parent, name = _parent_and_name(path)
    # Creating parents is the first mutating step: if anything after this raises
    # (or the caller abandons the transaction) every created parent rolls back.
    parent_row = ensure_directories(connection, parent)
    parent_id = parent_row["id"]

    row: Optional[sqlite3.Row] = None
    try:
        row = resolve(connection, path)
    except Missing:
        row = None

    existing = _check_conditions(row, digest, if_match, if_absent)

    if existing is not None:
        connection.execute(
            "UPDATE entries SET content = ?, updated_at = ? WHERE id = ?",
            (content, _utcnow_iso(), existing["id"]),
        )
    else:
        stamp = _utcnow_iso()
        connection.execute(
            "INSERT INTO entries "
            "(parent_id, name, kind, content, created_at, updated_at) "
            "VALUES (?, ?, 'file', ?, ?, ?)",
            (parent_id, name, content, stamp, stamp),
        )
        # A new direct child changes the *directly affected* directory's
        # membership, so its updated_at advances in the same transaction. Only
        # the immediate parent is updated, never every ancestor (spec §3).
        connection.execute(
            "UPDATE entries SET updated_at = ? WHERE id = ?",
            (stamp, parent_id),
        )

    final = resolve(connection, path)
    return WriteResult(entry=_entry_from_row(final, path), content_hash=digest)


def write_file(
    vault: "Vault",
    path: str,
    content: str,
    if_match: Optional[str] = None,
    if_absent: bool = False,
) -> WriteResult:
    """Public atomic conditional write of ``content`` at ``path``.

    The path is normalized before any resolution. The write runs through the
    vault's writer transaction, which acquires the write reservation
    (``BEGIN IMMEDIATE``) before the conditions are checked, and rolls back every
    created parent directory if the write raises. On success it returns a
    :class:`WriteResult`.
    """
    canonical = normalize_path(path)
    with vault.transaction(write=True):
        return write_in_transaction(
            vault.connection,
            canonical,
            content,
            if_match=if_match,
            if_absent=if_absent,
        )


def _utcnow_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
