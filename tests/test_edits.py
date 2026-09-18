"""Exact-text replacement parsing and planning tests for :mod:`memhub.edits`.

Run with the installed test environment, e.g.::

    .venv/bin/python -m pytest -q tests/test_edits.py

These tests exercise the pure ``parse_edits`` / ``apply_edits`` surface and the
:class:`~memhub.models.Edit` record, so they only import :mod:`memhub`, its
typed errors, and the new modules -- never the encoding detector.
"""

from __future__ import annotations

import pytest

from memhub.edits import apply_edits, parse_edits
from memhub.errors import Conflict, InvalidInput
from memhub.models import Edit


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
