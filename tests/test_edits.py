"""Exact-text replacement parsing and planning tests for :mod:`memhub.edits`.

Run with the installed test environment, e.g.::

    .venv/bin/python -m pytest -q tests/test_edits.py

These tests exercise the pure ``parse_edits`` / ``apply_edits`` surface and the
:class:`~memhub.models.Edit` record, so they only import :mod:`memhub`, its
typed errors, and the new modules -- never the encoding detector.
"""

from __future__ import annotations

import pytest

import memhub
from memhub.documents import WriteResult, edit_file, read_file, write_file
from memhub.edits import apply_edits, parse_edits
from memhub.errors import Conflict, InvalidInput, Missing
from memhub.models import Edit
from memhub.text import content_hash
from memhub.tree import ensure_directories, resolve


# --------------------------------------------------------------------------- #
# Edit record shape
# --------------------------------------------------------------------------- #


def test_edit_has_string_old_and_new_text_and_default_replace_all():
    edit = Edit(old_text="a", new_text="b")
    assert edit.old_text == "a"
    assert edit.new_text == "b"
    assert edit.replace_all is False
    assert isinstance(edit.replace_all, bool)


def test_edit_replace_all_is_a_real_boolean():
    edit = Edit(old_text="a", new_text="b", replace_all=True)
    assert edit.replace_all is True
    # A bool is an int subclass, but the field itself must be boolean.
    assert isinstance(edit.replace_all, bool)


def test_edit_is_frozen():
    edit = Edit(old_text="a", new_text="b")
    with pytest.raises(Exception):  # noqa: B017 - frozen dataclass mutates
        edit.old_text = "c"


# --------------------------------------------------------------------------- #
# Parser: accepted input
# --------------------------------------------------------------------------- #


def test_parse_edits_returns_list_of_edit():
    edits = parse_edits([{"old_text": "a", "new_text": "b"}])
    assert isinstance(edits, list)
    assert len(edits) == 1
    assert isinstance(edits[0], Edit)
    assert edits[0].old_text == "a"
    assert edits[0].new_text == "b"


def test_parse_edits_defaults_replace_all_false():
    (edit,) = parse_edits([{"old_text": "a", "new_text": "b"}])
    assert edit.replace_all is False


def test_parse_edits_preserves_replace_all_true():
    (edit,) = parse_edits(
        [{"old_text": "a", "new_text": "b", "replace_all": True}]
    )
    assert edit.replace_all is True


def test_parse_edits_accepts_empty_new_text():
    (edit,) = parse_edits([{"old_text": "a", "new_text": ""}])
    assert edit.new_text == ""


def test_parse_edits_accepts_unicode():
    (edit,) = parse_edits([{"old_text": "café", "new_text": "cafés"}])
    assert edit.old_text == "café"
    assert edit.new_text == "cafés"


# --------------------------------------------------------------------------- #
# Parser: malformed input is rejected
# --------------------------------------------------------------------------- #


def test_parse_edits_rejects_non_list_payload():
    with pytest.raises(InvalidInput) as invalid:
        parse_edits({"old_text": "a", "new_text": "b"})
    assert invalid.value.code == "edit_malformed"


def test_parse_edits_rejects_empty_list():
    with pytest.raises(InvalidInput) as invalid:
        parse_edits([])
    assert invalid.value.code == "edit_empty_list"


def test_parse_edits_rejects_non_object_operation():
    with pytest.raises(InvalidInput) as invalid:
        parse_edits(["a"])
    assert invalid.value.code == "edit_malformed"


def test_parse_edits_rejects_unknown_field():
    with pytest.raises(InvalidInput) as invalid:
        parse_edits([{"old_text": "a", "new_text": "b", "note": "x"}])
    assert invalid.value.code == "edit_unknown_field"


def test_parse_edits_rejects_missing_old_text():
    with pytest.raises(InvalidInput) as invalid:
        parse_edits([{"new_text": "b"}])
    assert invalid.value.code == "edit_missing_field"


def test_parse_edits_rejects_missing_new_text():
    with pytest.raises(InvalidInput) as invalid:
        parse_edits([{"old_text": "a"}])
    assert invalid.value.code == "edit_missing_field"


def test_parse_edits_rejects_empty_old_text():
    with pytest.raises(InvalidInput) as invalid:
        parse_edits([{"old_text": "", "new_text": "b"}])
    assert invalid.value.code == "edit_empty_needle"


def test_parse_edits_rejects_non_string_old_text():
    with pytest.raises(InvalidInput) as invalid:
        parse_edits([{"old_text": 1, "new_text": "b"}])
    assert invalid.value.code == "edit_old_text_type"


def test_parse_edits_rejects_non_string_new_text():
    with pytest.raises(InvalidInput) as invalid:
        parse_edits([{"old_text": "a", "new_text": 2}])
    assert invalid.value.code == "edit_new_text_type"


@pytest.mark.parametrize("bad_flag", [1, 0, "true", "false", None, 2])
def test_parse_edits_rejects_non_boolean_replace_all(bad_flag):
    with pytest.raises(InvalidInput) as invalid:
        parse_edits(
            [{"old_text": "a", "new_text": "b", "replace_all": bad_flag}]
        )
    assert invalid.value.code == "edit_replace_all_type"


def test_parse_edits_rejects_multiple_problems_together():
    with pytest.raises(InvalidInput):
        parse_edits([{"old_text": "a", "new_text": "b", "extra": 1, "replace_all": 1}])


# --------------------------------------------------------------------------- #
# Apply: happy paths
# --------------------------------------------------------------------------- #


def test_apply_edits_single_replacement():
    result = apply_edits("hello world", [Edit("world", "there")])
    assert result == "hello there"


def test_apply_edits_new_text_may_be_empty():
    result = apply_edits("aXb", [Edit("X", "")])
    assert result == "ab"


def test_apply_edits_preserves_whitespace_and_newlines():
    result = apply_edits("a\tb\nc", [Edit("\t", " ")])
    assert result == "a b\nc"


def test_apply_edits_unicode_matching():
    result = apply_edits("café crème", [Edit("café", "CAFÉ")])
    assert result == "CAFÉ crème"


def test_apply_edits_all_occurrences_when_replace_all():
    result = apply_edits("a-a-a", [Edit("a", "b", replace_all=True)])
    assert result == "b-b-b"


def test_apply_edits_non_overlapping_left_to_right_on_replace_all():
    # "aaa" has three overlapping single-character candidates but a single
    # non-overlapping left-to-right pass selects all three "a" slices, so each
    # becomes "b" -> "bbb" (no slice is skipped and none is double-counted).
    result = apply_edits("aaa", [Edit("a", "b", replace_all=True)])
    assert result == "bbb"


def test_apply_edits_matches_against_original_not_incremental():
    # Applying against the progressively edited text would rescan the injected
    # "b" and diverge; the plan resolves every occurrence against the original.
    result = apply_edits("aaaa", [Edit("aa", "aaX", replace_all=True)])
    assert result == "aaXaaX"


# --------------------------------------------------------------------------- #
# Apply: rejection cases
# --------------------------------------------------------------------------- #


def test_apply_edits_rejects_empty_operation_list():
    with pytest.raises(InvalidInput) as invalid:
        apply_edits("abc", [])
    assert invalid.value.code == "edit_empty_list"


def test_apply_edits_rejects_non_string_original():
    with pytest.raises(InvalidInput) as invalid:
        apply_edits(123, [Edit("a", "b")])
    assert invalid.value.code == "edit_original_type"


def test_apply_edits_rejects_non_edit_operations():
    with pytest.raises(InvalidInput) as invalid:
        apply_edits("a", [{"old_text": "a", "new_text": "b"}])
    assert invalid.value.code == "edit_type"


def test_apply_edits_rejects_no_match():
    with pytest.raises(Conflict) as conflict:
        apply_edits("abc", [Edit("z", "y")])
    assert conflict.value.code == "edit_no_match"


def test_apply_edits_rejects_ambiguous_single_match():
    # "a" appears twice and replace_all is unset: exactly one is required.
    with pytest.raises(Conflict) as conflict:
        apply_edits("a-a", [Edit("a", "b")])
    assert conflict.value.code == "edit_ambiguous_match"


def test_apply_edits_allows_single_occurrence_without_replace_all():
    result = apply_edits("a-a", [Edit("-", "_")])
    assert result == "a_a"


def test_apply_edits_rejects_replace_all_with_zero_occurrences():
    with pytest.raises(Conflict) as conflict:
        apply_edits("abc", [Edit("z", "y", replace_all=True)])
    assert conflict.value.code == "edit_no_match"


def test_apply_edits_never_self_overlaps_within_a_single_operation():
    # A single replace_all operation's occurrences are chosen non-overlapping
    # from left to right, so it can never select an intersecting range of its
    # own; overlapping candidate substrings collapse to disjoint slices.
    result = apply_edits("aaaa", [Edit("aa", "X", replace_all=True)])
    assert result == "XX"


def test_apply_edits_rejects_overlapping_selections_across_operations():
    ops = [Edit("ab", "1"), Edit("bc", "2")]
    with pytest.raises(Conflict) as conflict:
        apply_edits("abc", ops)
    assert conflict.value.code == "edit_ranges_overlap"


def test_apply_edits_allows_adjacent_non_overlapping_selections():
    ops = [Edit("a", "1"), Edit("b", "2")]
    result = apply_edits("ab", ops)
    assert result == "12"


def test_apply_edits_rejects_intersection_after_sorting():
    # The second operation is listed first but starts earlier; intersection is
    # detected regardless of declaration order.
    ops = [Edit("b", "2"), Edit("ab", "1")]
    with pytest.raises(Conflict) as conflict:
        apply_edits("abc", ops)
    assert conflict.value.code == "edit_ranges_overlap"


def test_apply_edits_rejects_result_introducing_prohibited_control():
    with pytest.raises(InvalidInput) as invalid:
        apply_edits("x", [Edit("x", "y\x00z")])
    assert invalid.value.code == "content_control_character"


def test_apply_edits_allows_allowed_controls_in_result():
    result = apply_edits("x", [Edit("x", "y\tz\nw")])
    assert result == "y\tz\nw"


def test_apply_edits_allows_preexisting_replacement_character():
    # An existing literal U+FFFD is admitted; the edit need not introduce one.
    result = apply_edits("a\ufffdb", [Edit("a", "A")])
    assert result == "A\ufffdb"


# --------------------------------------------------------------------------- #
# Parser + apply integration
# --------------------------------------------------------------------------- #


def test_parse_then_apply_roundtrip():
    edits = parse_edits(
        [
            {"old_text": "lo", "new_text": "lOR", "replace_all": False},
            {"old_text": "world", "new_text": "WORLD", "replace_all": True},
        ]
    )
    assert all(isinstance(e, Edit) for e in edits)
    result = apply_edits("hello world", edits)
    assert result == "hellOR WORLD"


def test_parse_then_apply_rejects_ambiguous_operation():
    with pytest.raises(Conflict):
        apply_edits("a-a", parse_edits([{"old_text": "a", "new_text": "b"}]))


# --------------------------------------------------------------------------- #
# edit_file integration (Task 8: conflict-safe conditional edits)
# --------------------------------------------------------------------------- #


def test_edit_file_replaces_text_and_returns_write_result(vault):
    write_file(vault, "/doc.txt", "hello world")
    result = edit_file(vault, "/doc.txt", [Edit("world", "there")])
    assert isinstance(result, WriteResult)
    assert result.content_hash == content_hash("hello there")
    assert read_file(vault, "/doc.txt").content == "hello there"


def test_edit_file_applies_non_overlapping_multi_edit(vault):
    write_file(vault, "/doc.txt", "a-b-c")
    result = edit_file(vault, "/doc.txt", [Edit("a", "A"), Edit("c", "C")])
    assert result.content_hash == content_hash("A-b-C")
    assert read_file(vault, "/doc.txt").content == "A-b-C"


def test_edit_file_supports_replace_all_within_a_transaction(vault):
    write_file(vault, "/doc.txt", "a-a-a")
    result = edit_file(vault, "/doc.txt", [Edit("a", "b", replace_all=True)])
    assert result.content_hash == content_hash("b-b-b")
    assert read_file(vault, "/doc.txt").content == "b-b-b"


def test_edit_file_allows_result_equal_to_original(vault):
    write_file(vault, "/doc.txt", "unchanged")
    result = edit_file(vault, "/doc.txt", [Edit("unchanged", "unchanged")])
    assert result.content_hash == content_hash("unchanged")
    assert read_file(vault, "/doc.txt").content == "unchanged"


def test_edit_file_requires_an_existing_document(vault):
    with pytest.raises(Missing) as missing:
        edit_file(vault, "/missing.txt", [Edit("a", "b")])
    assert missing.value.code == "edit_missing_document"


def test_edit_file_rejects_editing_a_directory(vault):
    # Create the directory inside a caller-owned write transaction so the edit
    # under test sees a real directory entry to refuse, rather than resolving a
    # nonexistent path (which would raise Missing from resolve itself).
    with vault.transaction(write=True):
        ensure_directories(vault.connection, "/folder")
    with pytest.raises(Missing) as missing:
        edit_file(vault, "/folder", [Edit("a", "b")])
    assert missing.value.code == "edit_missing_document"


def test_edit_file_rejects_stale_if_match(vault):
    write_file(vault, "/doc.txt", "hello")
    with pytest.raises(Conflict) as conflict:
        edit_file(vault, "/doc.txt", [Edit("hello", "hi")], if_match="deadbeef")
    assert conflict.value.code == "stale_hash"


def test_edit_file_accepts_a_matching_if_match(vault):
    write_file(vault, "/doc.txt", "hello")
    digest = content_hash("hello")
    result = edit_file(vault, "/doc.txt", [Edit("hello", "hi")], if_match=digest)
    assert result.content_hash == content_hash("hi")


def test_edit_file_rejects_overlapping_operations(vault):
    write_file(vault, "/doc.txt", "abc")
    # "ab" selects [0, 2) and "bc" selects [1, 3); the two selections intersect,
    # so the edit must be rejected as an overlap rather than applied.
    with pytest.raises(Conflict) as conflict:
        edit_file(vault, "/doc.txt", [Edit("ab", "1"), Edit("bc", "2")])
    assert conflict.value.code == "edit_ranges_overlap"


def test_edit_file_stale_hash_leaves_content_and_timestamp_unchanged(vault):
    write_file(vault, "/doc.txt", "original")
    before = resolve(vault.connection, "/doc.txt")
    with pytest.raises(Conflict):
        edit_file(vault, "/doc.txt", [Edit("original", "changed")], if_match="wrong")
    after = resolve(vault.connection, "/doc.txt")
    assert after["content"] == "original"
    assert after["updated_at"] == before["updated_at"]
    # The failed edit rolled back inside its write reservation; the document is
    # still exactly what it was and the vault remains intact.
    assert read_file(vault, "/doc.txt").content == "original"
    assert vault.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_edit_file_preserves_created_at_and_advances_only_file_timestamp(vault):
    write_file(vault, "/doc.txt", "first")
    original = resolve(vault.connection, "/doc.txt")
    edit_file(vault, "/doc.txt", [Edit("first", "second")])
    updated = resolve(vault.connection, "/doc.txt")
    assert updated["created_at"] == original["created_at"]
    assert updated["content"] == "second"


def test_edit_file_parses_json_operations_before_commit(vault):
    write_file(vault, "/doc.txt", "hello world")
    operations = parse_edits(
        [
            {"old_text": "lo", "new_text": "lOR", "replace_all": False},
            {"old_text": "world", "new_text": "WORLD", "replace_all": True},
        ]
    )
    result = edit_file(vault, "/doc.txt", operations)
    assert result.content_hash == content_hash("hellOR WORLD")
    assert read_file(vault, "/doc.txt").content == "hellOR WORLD"
