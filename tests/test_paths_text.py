"""Canonical path and text-admission tests.

Run with the installed test environment, e.g.::

    .venv/bin/python -m pytest -q tests/test_paths_text.py

These tests import only :mod:`memhub.paths`, :mod:`memhub.text`, and the typed
errors, so they never pull the encoding detector. They cover the behaviour
described in the spec's virtual-path rules (section 3) and text-admission rules
(section 6): separator collapsing, absolute-only paths, rejected dot/control/
surrogate names, literal glob handling, case and Unicode-composition fidelity,
whitespace admission, NUL/C0 rejection, allowed replacement characters, CRLF
preservation, and complete-document UTF-8 hashing.
"""

from __future__ import annotations

import hashlib
import unicodedata

import pytest

from memhub.errors import InvalidInput
from memhub.paths import normalize_path
from memhub.text import content_hash, validate_text


# --- Path normalization ------------------------------------------------------


def test_repeated_separators_collapse_to_one():
    assert normalize_path("/a//b///c") == "/a/b/c"


def test_trailing_separator_is_normalized():
    assert normalize_path("/a/b/") == "/a/b"


def test_root_is_root():
    assert normalize_path("/") == "/"


def test_separator_only_is_root():
    assert normalize_path("///") == "/"


def test_normalization_is_idempotent():
    path = normalize_path("/a//b/")
    assert normalize_path(path) == path


def test_dot_components_are_rejected():
    with pytest.raises(InvalidInput):
        normalize_path("/a/./b")


def test_parent_components_are_rejected():
    with pytest.raises(InvalidInput):
        normalize_path("/a/../b")
    with pytest.raises(InvalidInput):
        normalize_path("/..")


def test_relative_paths_are_rejected():
    with pytest.raises(InvalidInput):
        normalize_path("a/b")
    with pytest.raises(InvalidInput):
        normalize_path("just-one")
    with pytest.raises(InvalidInput):
        normalize_path("")


def test_control_character_names_are_rejected():
    with pytest.raises(InvalidInput):
        normalize_path("/a\x01b")  # BEL / C0 control
    with pytest.raises(InvalidInput):
        normalize_path("/tab\there")  # tab is a C0 control in a name
    with pytest.raises(InvalidInput):
        normalize_path("/nul\x00there")  # NUL


def test_unicode_surrogates_are_rejected():
    with pytest.raises(InvalidInput):
        normalize_path("/\ud800component")


def test_literal_glob_characters_pass_through_unchanged():
    assert normalize_path("/*.txt") == "/*.txt"
    assert normalize_path("/a/[0-9]?*") == "/a/[0-9]?*"


def test_tilde_and_environment_references_are_not_expanded():
    assert normalize_path("/~user/file") == "/~user/file"
    assert normalize_path("/$HOME/file") == "/$HOME/file"


def test_names_preserve_case():
    assert normalize_path("/Foo") == "/Foo"
    assert normalize_path("/Foo") != normalize_path("/foo")


def test_composed_and_decomposed_names_are_not_normalized():
    base = "cafe\u0301"  # "cafe" + combining acute accent
    composed = "/" + unicodedata.normalize("NFC", base)
    decomposed = "/" + unicodedata.normalize("NFD", base)
    assert normalize_path(composed) == composed
    assert normalize_path(decomposed) == decomposed
    assert composed != decomposed  # Unicode normalization was intentionally skipped


def test_non_string_input_is_rejected():
    with pytest.raises(InvalidInput):
        normalize_path(None)  # type: ignore[arg-type]


# --- Content admission -------------------------------------------------------


def test_allowed_whitespace_is_admitted():
    validate_text("  \n\t\r\f  text\n")  # must not raise


def test_empty_content_is_admitted():
    validate_text("")  # must not raise


def test_line_oriented_controls_are_allowed():
    validate_text("\t\n\r\f")  # must not raise


def test_nul_is_rejected():
    with pytest.raises(InvalidInput) as caught:
        validate_text("a\x00b")
    assert caught.value.exit_status == 2


def test_other_c0_controls_are_rejected():
    for code in (0x01, 0x07, 0x1B, 0x1F):
        with pytest.raises(InvalidInput):
            validate_text(f"x{chr(code)}y")


def test_literal_replacement_character_is_admitted():
    validate_text("prefix\ufffdsuffix")  # must not raise


def test_crlf_is_preserved():
    validate_text("line1\r\nline2\r\n")  # must not raise


def test_non_string_content_is_rejected():
    with pytest.raises(InvalidInput):
        validate_text(b"bytes")  # type: ignore[arg-type]


# --- Content hashing ---------------------------------------------------------


def test_hash_matches_sha256_of_utf8():
    assert content_hash("hello") == hashlib.sha256(b"hello").hexdigest()


def test_hash_covers_complete_document():
    document = "line1\nline2\nline3"
    assert content_hash(document) == hashlib.sha256(document.encode("utf-8")).hexdigest()


def test_hash_includes_carriage_returns():
    document = "a\r\nb"
    assert content_hash(document) == hashlib.sha256(document.encode("utf-8")).hexdigest()


def test_hash_is_a_sixty_four_char_hex_string():
    digest = content_hash("anything")
    assert len(digest) == 64
    int(digest, 16)  # raises ValueError if the digest is not pure hex


def test_distinct_documents_have_distinct_hashes():
    assert content_hash("one") != content_hash("two")


def test_non_string_content_is_rejected_for_hash():
    with pytest.raises(InvalidInput):
        content_hash(b"bytes")  # type: ignore[arg-type]
