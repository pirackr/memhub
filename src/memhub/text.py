"""Content admission and UTF-8 content hashing.

The storage library stores decoded Unicode text. These helpers answer the two
questions a caller must resolve before a document is persisted:

* Is this text acceptable to store? :func:`validate_text` enforces a small,
  deliberate text-admission rule: NUL and C0 control characters are rejected
  except tab, line feed, form feed, and carriage return. Everything else —
  including an already-present U+FFFD replacement character, CRLF pairs, and any
  non-control Unicode — is admitted verbatim.
* What does this document contain? :func:`content_hash` returns the SHA-256 hex
  digest of the document encoded as complete UTF-8. The digest covers the whole
  document, so a partial read never changes it.

Neither function rewrites the text it is given.

This is an explicit text-admission heuristic, not a universal binary detector:
it runs *after* Unicode decoding and only forbids NUL/C0 controls. An existing
literal U+FFFD is allowed on purpose so the decoder cannot hide errors behind a
replacement character.
"""

from __future__ import annotations

import hashlib

from .errors import InvalidInput

__all__ = ["validate_text", "content_hash"]

# C0 controls that are ordinary in text: tab, line feed, form feed, carriage return.
_ALLOWED_CONTROLS = frozenset({0x09, 0x0A, 0x0C, 0x0D})


def validate_text(content: str) -> None:
    """Admit ``content`` for storage, or raise :class:`InvalidInput`.

    Returns nothing on success. Rejects a non-string input and any string that
    contains a NUL or C0 control character other than tab (``0x09``), line feed
    (``0x0A``), form feed (``0x0C``), or carriage return (``0x0D``). An existing
    U+FFFD replacement character is allowed, and CRLF pairs are preserved. The
    input is not modified.
    """
    if not isinstance(content, str):
        raise InvalidInput(
            "content_type",
            f"document content must be decoded text, got {type(content).__name__}",
        )
    for char in content:
        code = ord(char)
        if code < 0x20 and code not in _ALLOWED_CONTROLS:
            raise InvalidInput(
                "content_control_character",
                f"content contains rejected control character U+{code:04X}",
            )


def content_hash(content: str) -> str:
    """Return the SHA-256 hex digest of ``content`` encoded as complete UTF-8.

    The digest covers the entire document. Raises :class:`InvalidInput` for a
    non-string input. The input is not modified.
    """
    if not isinstance(content, str):
        raise InvalidInput(
            "content_type",
            f"document content must be decoded text, got {type(content).__name__}",
        )
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
