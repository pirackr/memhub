"""Encoding-detection and strict-decoding tests.

Run with the installed test environment, e.g.::

    .venv/bin/python -m pytest -q tests/test_encoding.py

These tests import only :mod:`memhub.encoding`, :mod:`memhub.models`, and the
typed errors, so they never pull the encoding detector unless a test reaches the
chardet path on purpose. They cover the behaviour described in the spec's import
and encoding rules (section 6): Unicode BOM order (UTF-32 before the overlapping
UTF-16 signatures), strict whole-file UTF-8, canonical codec labels, strict
legacy decoding behind a lazy/pinned detector, uncertain and unknown detection,
decode failure, literal U+FFFD admission, newline preservation, binary
admission, and that ordinary imports never load the detector.

Detector-dependent behaviour is driven through the deterministic
``memhub.encoding._detect_encoding`` seam so the suite does not depend on the
exact guesses of a particular chardet build; one real-fixture test exercises the
pinned detector directly.
"""

from __future__ import annotations

from contextlib import contextmanager

import sys

import pytest

import memhub.encoding as encoding
from memhub.encoding import DecodedText, decode_bytes
from memhub.errors import Unsupported

# chardet is a pinned optional runtime dependency, lazy-loaded behind the import
# layer. Prefer to exercise it directly; if the environment never installed it,
# skip that one real-fixture case rather than error.
try:
    import chardet  # noqa: F401

    _HAS_CHARDET = True
except ImportError:
    _HAS_CHARDET = False


@contextmanager
def _no_chardet_state():
    """Snapshot, clear, and restore ``chardet`` in ``sys.modules``.

    Determinism tests assert that ordinary import/BOM/UTF-8 paths never load the
    detector. Because the single real-fixture test imports ``chardet`` and never
    unimports it, leaving it resident in ``sys.modules`` for the rest of the
    run, those assertions must be checked against a clean module state rather
    than the order-dependent, polluted state of the running interpreter.
    """
    saved = sys.modules.get("chardet")
    sys.modules.pop("chardet", None)
    try:
        yield
    finally:
        if saved is not None:
            sys.modules["chardet"] = saved


# --- BOM dispatch ----------------------------------------------------------


def test_utf32_le_bom_is_decoded_and_bom_consumed():
    data = b"\xff\xfe\x00\x00" + "A".encode("utf-32-le")
    result = decode_bytes(data)
    assert isinstance(result, DecodedText)
    assert result.encoding == "utf-32-le"
    assert result.text == "A"
    assert result.confidence == 1.0


def test_utf32_be_bom_is_decoded_and_bom_consumed():
    data = b"\x00\x00\xfe\xff" + "A".encode("utf-32-be")
    result = decode_bytes(data)
    assert result.encoding == "utf-32-be"
    assert result.text == "A"
    assert result.confidence == 1.0


def test_utf16_le_bom_is_decoded_and_bom_consumed():
    data = b"\xff\xfe" + "A".encode("utf-16-le")
    result = decode_bytes(data)
    assert result.encoding == "utf-16-le"
    assert result.text == "A"
    assert result.confidence == 1.0


def test_utf16_be_bom_is_decoded_and_bom_consumed():
    data = b"\xfe\xff" + "A".encode("utf-16-be")
    result = decode_bytes(data)
    assert result.encoding == "utf-16-be"
    assert result.text == "A"
    assert result.confidence == 1.0


def test_utf32_le_is_dispatched_before_utf16_le():
    # Leading bytes ff fe are the UTF-16-LE BOM *and* the UTF-32-LE BOM; UTF-32
    # must win so the file is not misread as 16-bit.
    data = b"\xff\xfe\x00\x00" + "A".encode("utf-32-le")
    result = decode_bytes(data)
    assert result.encoding == "utf-32-le"
    assert result.text == "A"
    assert not result.text.startswith("\ufeff")


def test_a_genuine_utf16_le_file_is_not_read_as_utf32():
    # ff fe 41 00 is exactly two UTF-16-LE code units worth (one char + BOM);
    # it has no third/fourth byte, so UTF-32 dispatch would fail here.
    data = b"\xff\xfe\x41\x00"
    result = decode_bytes(data)
    assert result.encoding == "utf-16-le"
    assert result.text == "A"


# --- UTF-8 -----------------------------------------------------------------


def test_valid_utf8_is_decoded_and_labeled_utf8():
    result = decode_bytes("héllo".encode("utf-8"))
    assert result.encoding == "utf-8"
    assert result.text == "héllo"
    assert result.confidence == 1.0


def test_ascii_is_treated_as_utf8():
    result = decode_bytes(b"plain ascii text")
    assert result.encoding == "utf-8"
    assert result.text == "plain ascii text"
    assert result.confidence == 1.0


def test_utf8_bom_is_consumed():
    data = b"\xef\xbb\xbf" + "hi".encode("utf-8")
    result = decode_bytes(data)
    assert result.encoding == "utf-8"
    assert result.text == "hi"
    assert not result.text.startswith("\ufeff")


def test_empty_bytes_decode_as_utf8():
    result = decode_bytes(b"")
    assert result.encoding == "utf-8"
    assert result.text == ""
    assert result.confidence == 1.0


# --- Detector seam: legacy, uncertain, unknown, failure --------------------


def test_legacy_codec_decoded_strictly_with_canonical_label(monkeypatch):
    monkeypatch.setattr(
        encoding, "_detect_encoding", lambda data: {"encoding": "windows-1252", "confidence": 0.99}
    )
    data = b"caf\xe9"  # "café" in windows-1252
    result = decode_bytes(data)
    assert result.encoding == "cp1252"  # canonical, not the detector's label
    assert result.text == "café"
    assert result.confidence == 0.99


def test_uncertain_guess_still_decodes_strictly(monkeypatch):
    # Confidence below the 0.80 warning threshold still decodes; the importer is
    # what warns, not decode_bytes.
    monkeypatch.setattr(
        encoding, "_detect_encoding", lambda data: {"encoding": "iso-8859-1", "confidence": 0.35}
    )
    data = b"caf\xe9"
    result = decode_bytes(data)
    assert result.encoding == "latin-1"
    assert result.text == "café"
    assert 0.0 <= result.confidence < 0.80


def test_unknown_encoding_is_rejected(monkeypatch):
    monkeypatch.setattr(
        encoding, "_detect_encoding", lambda data: {"encoding": None, "confidence": 0.0}
    )
    with pytest.raises(Unsupported):
        decode_bytes(b"\x89\x50\x4e\x47\x09\x0a\x1a\x0a")


def test_detectable_but_undeclared_encoding_is_rejected(monkeypatch):
    # A candidate that exists yet cannot be decoded as a Python codec is refused.
    monkeypatch.setattr(
        encoding, "_detect_encoding", lambda data: {"encoding": "not-a-real-codec", "confidence": 1.0}
    )
    with pytest.raises(Unsupported):
        decode_bytes(b"\x81\x82\x83")


def test_decode_failure_is_rejected():
    # UTF-16-LE BOM + one trailing byte -> odd number of bytes -> strict decode
    # fails before any text-admission step.
    data = b"\xff\xfe\x41"
    with pytest.raises(Unsupported):
        decode_bytes(data)


def test_detector_decode_failure_is_rejected(monkeypatch):
    monkeypatch.setattr(
        encoding, "_detect_encoding", lambda data: {"encoding": "utf-16-le", "confidence": 0.9}
    )
    # Invalid UTF-8 (reaches the detector) and odd-length for UTF-16-LE.
    with pytest.raises(Unsupported):
        decode_bytes(b"\x81\x82\x83")


# --- Text admission --------------------------------------------------------


def test_literal_replacement_character_is_admitted():
    result = decode_bytes("x﻿y".encode("utf-8"))
    assert result.text == "x﻿y"


def test_literal_replacement_character_admitted_via_detector(monkeypatch):
    # A literal U+FFFD lies outside the 0x00-0xFF range, so it cannot be produced
    # by a latin-1 decode; "x﻿y".encode("latin-1") is impossible (the original
    # bug). Instead reach the detector decode seam with data that fails strict
    # UTF-8, patch the seam to return a document that already holds a genuine
    # U+FFFD, and prove the shared text-admission check still admits it here.
    monkeypatch.setattr(
        encoding,
        "_decode_with_detector",
        lambda data: ("x�y", "latin-1", 0.9),
    )
    result = decode_bytes(b"\x81\x82\x83")
    assert result.text == "x�y"


def test_binary_with_nul_is_rejected():
    with pytest.raises(Unsupported):
        decode_bytes(b"\x00\x01\x02")


def test_binary_controls_rejected_via_detector(monkeypatch):
    monkeypatch.setattr(
        encoding, "_detect_encoding", lambda data: {"encoding": "latin-1", "confidence": 0.9}
    )
    # 0x01 is a disallowed C0 control after decoding.
    with pytest.raises(Unsupported):
        decode_bytes(b"\x81\x01\x82")


# --- Newlines --------------------------------------------------------------


def test_newlines_are_preserved_verbatim():
    text = "line1\nline2\r\nline3"
    result = decode_bytes(text.encode("utf-8"))
    assert result.text == text


def test_crlf_preserved_through_detector(monkeypatch):
    monkeypatch.setattr(
        encoding, "_detect_encoding", lambda data: {"encoding": "windows-1252", "confidence": 0.9}
    )
    text = "café\r\nsteak\r\n"
    data = text.encode("windows-1252")
    result = decode_bytes(data)
    assert result.text == text


# --- Real detector (pinned chardet), lenient -------------------------------


@pytest.mark.skipif(not _HAS_CHARDET, reason="chardet is a pinned optional runtime dependency")
def test_real_latin1_fixture_roundtrips():
    # Exercises the pinned detector directly; assert the reported codec both is
    # a plausible Latin codec and reproduces the exact source bytes.
    data = b"caf\xe9"
    result = decode_bytes(data)
    assert isinstance(result, DecodedText)
    assert result.encoding in ("cp1252", "latin-1")
    assert result.text.encode(result.encoding) == data


# --- Determinism / detector isolation --------------------------------------


def test_importing_memhub_does_not_load_chardet():
    with _no_chardet_state():
        import memhub  # noqa: F401
        import memhub.encoding  # noqa: F401

        assert "chardet" not in sys.modules


def test_bom_and_utf8_paths_never_load_chardet():
    with _no_chardet_state():
        encoding.decode_bytes(b"\xff\xfe\x00\x00" + "A".encode("utf-32-le"))
        encoding.decode_bytes(b"plain ascii text")
        assert "chardet" not in sys.modules


# --- Record shape ----------------------------------------------------------


def test_decodedtext_is_immutable():
    result = decode_bytes(b"hi")
    with pytest.raises(AttributeError):
        result.text = "x"  # type: ignore[misc]


def test_non_bytes_source_is_rejected():
    with pytest.raises(Unsupported):
        decode_bytes("not bytes")  # type: ignore[arg-type]
