"""Exact-text replacement parsing and planning.

A document edit is a list of :class:`~memhub.models.Edit` operations. This module
answers the two questions a caller resolves before an edit is committed:

* :func:`parse_edits` turns decoded JSON (a Python list of operation objects)
  into validated :class:`~memhub.models.Edit` values. It enforces a strict
  field set and types, rejecting unknown fields, missing fields, empty
  ``old_text`` values, and non-boolean ``replace_all`` flags.
* :func:`apply_edits` resolves the operations against a single original document
  and returns the spliced result. Every occurrence selection is discovered
  against the original text (never against progressively edited text), the
  selected ranges must not intersect, and the spliced result is re-admitted so
  an edit cannot introduce a prohibited control character.

Both functions never touch storage; they plan edits only. The transactional
commit is the caller's responsibility (see
:mod:`memhub.documents` in the next task).
"""

from __future__ import annotations

from typing import Any, Iterable

from .errors import Conflict, InvalidInput
from .models import Edit
from .text import validate_text

__all__ = ["parse_edits", "apply_edits"]

# The only fields an edit operation may carry. Anything else is rejected.
_ALLOWED_FIELDS = frozenset({"old_text", "new_text", "replace_all"})


def _as_edit_item(operation: Any) -> Edit:
    """Build a validated :class:`Edit` from a single decoded operation object.

    ``operation`` is one member of a decoded-JSON array: a mapping with at most
    the keys ``old_text``, ``new_text``, and ``replace_all``. Raises
    :class:`InvalidInput` for any structural or type violation. The returned edit
    carries a boolean ``replace_all`` and non-empty string ``old_text``.
    """
    if not isinstance(operation, dict):
        raise InvalidInput(
            "edit_malformed",
            f"each edit operation must be an object, got {type(operation).__name__}",
        )

    unknown = set(operation.keys()).difference(_ALLOWED_FIELDS)
    if unknown:
        raise InvalidInput(
            "edit_unknown_field",
            f"edit operation carries unknown field(s): {', '.join(sorted(unknown))}",
        )

    if "old_text" not in operation:
        raise InvalidInput(
            "edit_missing_field",
            "edit operation is missing required field 'old_text'",
        )
    if "new_text" not in operation:
        raise InvalidInput(
            "edit_missing_field",
            "edit operation is missing required field 'new_text'",
        )

    old_text = operation["old_text"]
    if not isinstance(old_text, str):
        raise InvalidInput(
            "edit_old_text_type",
            f"old_text must be a string, got {type(old_text).__name__}",
        )
    if old_text == "":
        raise InvalidInput(
            "edit_empty_needle",
            "old_text must be a non-empty string",
        )

    new_text = operation["new_text"]
    if not isinstance(new_text, str):
        raise InvalidInput(
            "edit_new_text_type",
            f"new_text must be a string, got {type(new_text).__name__}",
        )

    if "replace_all" in operation:
        replace_all = operation["replace_all"]
        if not isinstance(replace_all, bool):
            raise InvalidInput(
                "edit_replace_all_type",
                f"replace_all must be a boolean, got {type(replace_all).__name__}",
            )
    else:
        replace_all = False

    return Edit(old_text=old_text, new_text=new_text, replace_all=replace_all)


def parse_edits(payload: Any) -> list[Edit]:
    """Validate decoded-JSON edit input and return a list of :class:`Edit`.

    ``payload`` is *decoded* JSON: a Python list of operation objects, as
    produced by ``json.loads`` on an edit array. An empty list, a non-list, an
    operation that is not an object, an unknown field, a missing
    ``old_text``/``new_text``, an empty ``old_text``, a non-string ``new_text``,
    or a non-boolean ``replace_all`` all raise :class:`InvalidInput` (exit status
    2). The returned list is guaranteed non-empty.
    """
    if not isinstance(payload, list):
        raise InvalidInput(
            "edit_malformed",
            f"edit payload must be a JSON array, got {type(payload).__name__}",
        )
    if not payload:
        raise InvalidInput(
            "edit_empty_list",
            "edit operation list must contain at least one operation",
        )

    return [_as_edit_item(operation) for operation in payload]


def _nonoverlapping_occurrences(text: str, needle: str) -> list[tuple[int, int]]:
    """Return the ``(start, end)`` index ranges of ``needle`` in ``text``.

    Occurrences are discovered left to right and never overlap: the next search
    begins immediately after the previous match ends.
    """
    ranges: list[tuple[int, int]] = []
    if not needle:
        return ranges
    start = 0
    while True:
        found = text.find(needle, start)
        if found < 0:
            break
        ranges.append((found, found + len(needle)))
        start = found + len(needle)
    return ranges


def _select_ranges(edit: Edit, original: str) -> list[tuple[int, int]]:
    """Return the selected index ranges for ``edit`` inside ``original``.

    A single-match operation must match exactly one non-overlapping occurrence;
    more than one is an ambiguous conflict. A ``replace_all`` operation selects
    every non-overlapping occurrence and must match at least one. Raises
    :class:`Conflict` when either condition is unmet.
    """
    ranges = _nonoverlapping_occurrences(original, edit.old_text)
    if not ranges:
        raise Conflict(
            "edit_no_match",
            f"old_text {edit.old_text!r} does not appear in the document",
        )
    if not edit.replace_all and len(ranges) != 1:
        raise Conflict(
            "edit_ambiguous_match",
            f"old_text {edit.old_text!r} occurs {len(ranges)} times; "
            "expected exactly one unless replace_all is true",
        )
    return ranges


def _check_no_overlap(all_ranges: list[tuple[int, int]]) -> None:
    """Reject any two selected ranges that intersect.

    ``all_ranges`` is a single list of every selection across all operations,
    so a self-overlap (impossible here, since each operation's selections are
    non-overlapping) or a cross-operation overlap is caught the same way.
    """
    ordered = sorted(all_ranges)
    for (start_a, end_a), (start_b, end_b) in zip(ordered, ordered[1:]):
        if start_b < end_a:
            raise Conflict(
                "edit_ranges_overlap",
                "edit selections intersect; overlapping replacements are rejected",
            )


def apply_edits(original: str, operations: Iterable[Edit]) -> str:
    """Return the document that results from applying ``operations``.

    ``original`` is the current document text. ``operations`` is a non-empty
    sequence of :class:`~memhub.models.Edit` values. Every occurrence selection
    is resolved against ``original`` (never against progressively edited text),
    all selections must be mutually non-overlapping, and the spliced result is
    re-admitted through :func:`memhub.text.validate_text`.

    Raises :class:`InvalidInput` for a non-string original, an empty operation
    list, an operation that is not an :class:`Edit`, or a result that introduces
    a prohibited control character. Raises :class:`Conflict` for an unmatched,
    ambiguous, or overlapping edit. The returned string is an admitted result.
    """
    if not isinstance(original, str):
        raise InvalidInput(
            "edit_original_type",
            f"original document must be a string, got {type(original).__name__}",
        )

    operations = list(operations)
    if not operations:
        raise InvalidInput(
            "edit_empty_list",
            "edit operation list must contain at least one operation",
        )
    for edit in operations:
        if not isinstance(edit, Edit):
            raise InvalidInput(
                "edit_type",
                f"each operation must be an Edit, got {type(edit).__name__}",
            )

    all_ranges: list[tuple[int, int]] = []
    selections: list[tuple[int, int, str]] = []
    for edit in operations:
        ranges = _select_ranges(edit, original)
        for start, end in ranges:
            all_ranges.append((start, end))
            selections.append((start, end, edit.new_text))

    _check_no_overlap(all_ranges)

    result_parts: list[str] = []
    position = 0
    for start, end, replacement in sorted(selections):
        result_parts.append(original[position:start])
        result_parts.append(replacement)
        position = end
    result_parts.append(original[position:])
    result = "".join(result_parts)

    # The spliced result must be admitted text too: an edit cannot smuggle a
    # prohibited control character past admission by writing it into new_text.
    validate_text(result)
    return result
