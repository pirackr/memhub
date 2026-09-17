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

:func:`read_file` returns a whole document or a one-based line range through a
single snapshot transaction. Lines are separated by LF only; carriage returns
are preserved, no text is normalized, and a trailing newline adds no empty
line. The reported hash covers the complete stored document even for a partial
read.

The storage library neither starts nor commits the surrounding transaction in
:func:`write_in_transaction`; the caller owns those boundaries. In
:func:`write_file` the :class:`~memhub.Vault` transaction owns them and rolls
back every created parent when the write raises.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Optional, Tuple

from .errors import Conflict, InvalidInput, Missing
from .models import Entry, ReadResult, WriteResult
from .paths import normalize_path
from .text import content_hash, validate_text
from .tree import DIRECTORY, FILE, ensure_directories, resolve

if TYPE_CHECKING:
    from .vault import Vault

__all__ = [
    "Entry",
    "ReadResult",
    "WriteResult",
    "read_file",
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


def _validate_range(
    start_line: int, lines: Optional[int]
) -> Tuple[int, Optional[int]]:
    """Validate the one-based line-range arguments of :func:`read_file`.

    ``start_line`` must be a positive integer. ``lines`` is optional but, when
    present, must also be a positive integer. ``bool`` is rejected even though
    it is an ``int`` subclass. Raises :class:`InvalidInput` otherwise; the
    arguments are returned unchanged on success.
    """
    if isinstance(start_line, bool) or not isinstance(start_line, int):
        raise InvalidInput(
            "invalid_start_line",
            "start_line must be an integer",
        )
    if start_line < 1:
        raise InvalidInput(
            "invalid_start_line",
            f"start_line must be at least 1, got {start_line}",
        )
    if lines is not None:
        if isinstance(lines, bool) or not isinstance(lines, int):
            raise InvalidInput(
                "invalid_lines",
                "lines must be an integer",
            )
        if lines < 1:
            raise InvalidInput(
                "invalid_lines",
                f"lines must be at least 1, got {lines}",
            )
    return start_line, lines


def _line_starts(content: str) -> tuple[int, list[int]]:
    """Return ``(total_lines, line_starts)`` for ``content``.

    ``line_starts[k]`` is the character offset at which line ``k + 1`` (1-based)
    begins. ``total_lines`` is the number of lines: an empty document has zero,
    and a trailing line feed does not add a final empty line. Every returned
    start is a real line start, so the substring for line ``L`` is
    ``content[line_starts[L - 1]:<next start or len(content)>``.
    """
    if content == "":
        return 0, []
    starts = [0]
    for index, char in enumerate(content):
        if char == "\n":
            starts.append(index + 1)
    # A trailing newline pushes a final start equal to len(content); that is the
    # dropped empty line, so drop it and treat len(content) as the last line end.
    if content.endswith("\n"):
        starts.pop()
    return len(starts), starts


def read_file(
    vault: "Vault",
    path: str,
    start_line: int = 1,
    lines: Optional[int] = None,
) -> ReadResult:
    """Read a whole document or a one-based line range from ``path``.

    ``start_line`` is one-based and defaults to ``1``. ``lines`` is an optional
    positive count; when ``None`` the remainder of the document from
    ``start_line`` is returned. The line range is clamped to the document, so a
    ``start_line`` past the end (or an ``lines`` count beyond the end) returns
    empty content with a null ``end_line`` rather than an error.

    The document is read through a single snapshot (``BEGIN``) transaction so
    the resolved row and its content are seen together. Text is returned exactly
    as stored: lines are separated by LF only, carriage returns are preserved,
    no normalization is applied, and a trailing newline is part of the last
    line rather than an appended empty line. The returned
    :class:`ReadResult.content_hash` covers the *complete* stored document even
    for a partial read.

    The path is normalized before resolution. A missing path raises
    :class:`Missing`; reading a directory raises :class:`Conflict`. A
    ``start_line`` below ``1`` or a non-positive / non-integer ``lines`` raises
    :class:`InvalidInput`.
    """
    canonical = normalize_path(path)
    start_line, lines = _validate_range(start_line, lines)

    with vault.transaction(write=False):
        row = resolve(vault.connection, canonical)
        if row["kind"] != FILE:
            raise Conflict(
                "read_directory",
                f"path {canonical!r} is a directory, not a file",
            )

        content = row["content"] if row["content"] is not None else ""
        total_lines, line_starts = _line_starts(content)
        entry = _entry_from_row(row, canonical)

        if start_line > total_lines:
            # An empty range: no lines returned and the range end is unknown.
            return ReadResult(
                entry=entry,
                content="",
                content_hash=content_hash(content),
                start_line=start_line,
                end_line=None,
                has_more=False,
            )

        if lines is not None:
            requested_end = start_line + lines - 1
        else:
            requested_end = total_lines
        has_more = requested_end < total_lines
        end_line = requested_end if has_more else total_lines

        start_offset = line_starts[start_line - 1]
        end_offset = len(content) if not has_more else line_starts[end_line]
        body = content[start_offset:end_offset]

        return ReadResult(
            entry=entry,
            content=body,
            content_hash=content_hash(content),
            start_line=start_line,
            end_line=end_line,
            has_more=has_more,
        )


def _utcnow_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
