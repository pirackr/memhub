"""Encoding detection and strict decoding for imported text.

Import reads raw host bytes whose encoding is unknown. This module turns those
bytes into decoded text without silently rewriting them: it never uses a
replacement-character or ``errors="ignore"`` decoder. Either the whole file
decodes cleanly under a chosen codec, or the source is rejected.

Detection order (spec section 6):

1. Recognize Unicode BOMs, testing the UTF-32 signatures before the overlapping
   UTF-16 signatures so a UTF-32-LE file is not misread as UTF-16-LE.
2. Accept valid UTF-8 directly (the common case for memhub, which stores UTF-8).
3. Otherwise fall back to the best candidate from a pinned detector
   (:func:`chardet`), isolated behind the import layer and lazy-loaded so normal
   CLI startup never pays its import cost.

The complete file is always decoded strictly. A detected or guessed encoding is
reported with its canonical codec label and the detector's confidence; the
deterministic BOM and UTF-8 cases report confidence ``1.0``. The decoded text is
then run through the shared text-admission check so a source that only *looks*
textual (decoded NUL/C0 controls) is rejected just like a write or edit would be.
"""

from __future__ import annotations

import codecs
from typing import Optional

from .errors import InvalidInput, Unsupported
from .models import DecodedText
from .text import validate_text

__all__ = ["DecodedText", "decode_bytes"]

# BOM signatures checked oldest/most-specific first. UTF-32 must precede the
# UTF-16 signatures that are its byte-for-byte prefixes; otherwise a UTF-32-LE
# file (``ff fe 00 00``) would be dispatched to UTF-16-LE and decode wrong.
# Each entry maps a leading signature to the explicit-endianness codec that
# decodes the *whole* file; the signature's own BOM character is reattached and
# stripped later by :func:`_finish`.
_BOM_TABLE: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)

# chardet's canonical label for a codec often differs from the name Python's
# ``codecs`` registry expects to *report*. Map chardet's output down to the
# canonical lowercase codec name this package publishes; anything unseen keeps
# its lowercased form after the mapping.
_CODEC_ALIASES: dict[str, str] = {
    "iso-8859-1": "latin-1",
    "iso-8859-2": "latin-2",
    "iso-8859-3": "latin-3",
    "iso-8859-4": "latin-4",
    "iso-8859-9": "latin-5",
    "iso-8859-15": "latin-15",
    "iso8859-1": "latin-1",
    "windows-1250": "cp1250",
    "windows-1251": "cp1251",
    "windows-1252": "cp1252",
    "windows-1256": "cp1256",
    "mswin-1252": "cp1252",
    "utf-16": "utf-16",
    "utf-32": "utf-32",
    "ascii": "ascii",
}

# The zero-width no-break space that a BOM decodes to. A single leading one is a
# consumed signature, not content.
_BOM_CHAR = chr(0xFEFF)


def decode_bytes(data: bytes) -> DecodedText:
    """Decode raw source ``data`` and return a :class:`~memhub.models.DecodedText`.

    The complete file is decoded strictly; a source that does not fully decode,
    or whose decoded text fails the shared text-admission check, raises
    :class:`~memhub.errors.Unsupported` (exit status 5).
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise Unsupported(
            "bytes_type",
            f"source must be raw bytes, got {type(data).__name__}",
        )
    data = bytes(data)

    codec = _detect_bom_codec(data)
    if codec is not None:
        try:
            text = data.decode(codec)  # strict; BOM reattaches, stripped below
        except (UnicodeDecodeError, LookupError, ValueError) as exc:
            raise Unsupported(
                "decode_failed",
                f"strict decode as {codec!r} failed: {exc}",
            ) from exc
        # A recognized BOM is a consumed signature, so its leading U+FEFF is
        # always stripped here.
        return _finish(text, codec, 1.0, consume_bom=True)

    # Strict UTF-8. Consume the encoding's signature BOM only when the raw
    # bytes actually begin with the UTF-8 BOM (EF BB BF); a later or non-BOM
    # U+FEFF that decodes out of ordinary UTF-8 text must be preserved.
    utf8_bom = data.startswith(b"\xef\xbb\xbf")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text, codec, confidence = _decode_with_detector(data)
        # The detector path consumes no signature: any leading U+FEFF is
        # genuine content and is preserved.
        return _finish(text, codec, confidence, consume_bom=False)
    return _finish(text, "utf-8", 1.0, consume_bom=utf8_bom)


def _finish(text: str, encoding: str, confidence: float, consume_bom: bool) -> DecodedText:
    """Admit ``text``, optionally strip a consumed signature BOM, and return it.

    ``consume_bom`` selects whether a single leading U+FEFF is stripped as a
    consumed encoding signature. Per the spec, only a recognized BOM signature
    (a UTF-16/UTF-32 BOM, or a UTF-8 BOM seen in the raw bytes) is consumed;
    a genuine U+FEFF that decodes out of ordinary text is always preserved.

    Text admission runs through the shared :func:`~memhub.text.validate_text`.
    At this import boundary an admission failure means the *source is
    unsupported*, so the shared :class:`~memhub.errors.InvalidInput` (exit status
    2) is translated to :class:`~memhub.errors.Unsupported` (exit status 5)
    rather than escaping across the import boundary.
    """
    if consume_bom and text.startswith(_BOM_CHAR):
        text = text[1:]
    try:
        validate_text(text)
    except InvalidInput as exc:
        raise Unsupported(
            "invalid_input",
            f"imported text rejected by text admission: {exc.message}",
        ) from exc
    return DecodedText(text=text, encoding=encoding, confidence=confidence)


def _detect_bom_codec(data: bytes) -> Optional[str]:
    """Return the explicit-endianness codec for a leading BOM, or ``None``."""
    for signature, codec in _BOM_TABLE:
        if data.startswith(signature):
            return codec
    return None


def _decode_with_detector(data: bytes) -> tuple[str, str, float]:
    """Decode ``data`` strictly using the detector's best candidate.

    The detector is lazy-loaded so ordinary package import never imports
    ``chardet``. A missing detector, an unrecognised source, or a strict decode
    failure all raise :class:`~memhub.errors.Unsupported`; a low confidence is
    reported honestly and left for the importer to warn about.
    """
    detected = _detect_encoding(data)
    label = detected.get("encoding") if isinstance(detected, dict) else None
    confidence = detected.get("confidence") if isinstance(detected, dict) else None

    if not label:
        raise Unsupported(
            "no_encoding",
            "could not identify an encoding for the source text",
        )

    confidence_value = _as_confidence(confidence)

    codec = _canonicalize_codec(label)
    if codec is None:
        raise Unsupported(
            "unsupported_encoding",
            f"detected encoding {label!r} is not a decodable codec",
        )

    try:
        text = data.decode(codec)
    except (UnicodeDecodeError, LookupError, ValueError) as exc:
        raise Unsupported(
            "decode_failed",
            f"strict decode as {codec!r} failed: {exc}",
        ) from exc

    return text, codec, confidence_value


def _as_confidence(confidence: object) -> float:
    """Return ``confidence`` as a float, or refuse a nonnumeric value.

    A detector that returns something that is not a finite-ish number cannot be
    reported honestly, so map it to :class:`~memhub.errors.Unsupported` here
    rather than letting a ``float()`` coercion raise elsewhere. A missing
    (``None``) confidence is not garbage; it stays an honest zero.
    """
    if confidence is None:
        return 0.0
    try:
        return float(confidence)
    except (TypeError, ValueError) as exc:
        raise Unsupported(
            "bad_confidence",
            f"detector confidence {confidence!r} is not numeric",
        ) from None


def _canonicalize_codec(label: object) -> Optional[str]:
    """Return the canonical codec name for a detector label, or ``None``."""
    if not isinstance(label, str):
        return None
    key = label.strip().lower()
    if not key:
        return None
    codec = _CODEC_ALIASES.get(key, key)
    # Reject anything the standard library cannot actually decode as a codec.
    try:
        codecs.lookup(codec)
    except LookupError:
        return None
    return codec


def _detect_encoding(data: bytes) -> dict:
    """Run the pinned detector over ``data`` and return its result dict.

    ``chardet`` is imported here only, so the BOM and UTF-8 fast paths never
    pull it in. Callers that need a deterministic detector in tests can replace
    this function.
    """
    try:
        import chardet
    except ImportError as exc:  # pragma: no cover - environment guard
        raise Unsupported(
            "detector_unavailable",
            "the chardet encoding detector is required but not installed",
        ) from exc

    return chardet.detect(data)
