"""Result records for memhub storage-library operations.

These are the immutable value types the storage library returns to callers.
They intentionally mirror the columns a :class:`sqlite3.Row` exposes, but carry
an explicit virtual ``path`` (which the adjacency-list schema never stores) and
drop columns the public surface does not publish (the parent id, the content
itself, etc.).

The records are frozen dataclasses so a caller cannot mutate a value it was
handed; a fresh record is built for every operation result.

Neither record touches the host filesystem or decodes bytes. Sizes are UTF-8
byte counts, never character counts, matching the spec's "Entry size means
UTF-8 byte count" rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

__all__ = ["Entry", "WriteResult", "ReadResult"]


@dataclass(frozen=True)
class Entry:
    """A published view of a single vault entry.

    ``id`` is the integer primary key. ``path`` is the canonical virtual path of
    the entry. ``kind`` is ``"file"`` or ``"directory"``. ``size_bytes`` is the
    UTF-8 byte count of a file's content, or ``None`` for a directory.
    ``created_at`` and ``updated_at`` are UTC ISO-8601 strings.
    """

    id: int
    path: str
    kind: str
    size_bytes: Optional[int]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class WriteResult:
    """The result of a document write.

    ``entry`` is the resulting :class:`Entry`; ``content_hash`` is the SHA-256
    hex digest of the complete written content.
    """

    entry: Entry
    content_hash: str


@dataclass(frozen=True)
class ReadResult:
    """The result of a :func:`memhub.documents.read_file` operation.

    ``entry`` is the :class:`Entry` for the document that was read. ``content``
    is the decoded slice returned to the caller; ``content_hash`` is the SHA-256
    hex digest of the *complete* stored document, so a partial read never
    changes it. ``start_line`` and ``end_line`` are the one-based inclusive
    line range covered by ``content``; ``end_line`` is ``None`` when the range
    is empty (for example a read past the end of the document). ``has_more``
    reports whether additional lines remain after ``end_line``.
    """

    entry: Entry
    content: str
    content_hash: str
    start_line: int
    end_line: Optional[int]
    has_more: bool
