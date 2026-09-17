"""Conditional atomic write tests for :mod:`memhub.documents`.

Run with the installed test environment, e.g.::

    .venv/bin/python -m pytest -q tests/test_documents.py

These tests only import :mod:`memhub` and its typed errors, so they never pull
the encoding detector.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

import memhub
from memhub.documents import (
    Entry,
    WriteResult,
    write_file,
    write_in_transaction,
)
from memhub.errors import Conflict, InvalidInput, Missing
from memhub.text import content_hash
from memhub.tree import DIRECTORY, FILE, resolve


def _count_under_root(vault: memhub.Vault) -> int:
    return vault.connection.execute(
        "SELECT COUNT(*) FROM entries WHERE parent_id IS NULL"
    ).fetchone()[0]


def _count_under_parent(vault: memhub.Vault, name: str) -> int:
    try:
        parent = resolve(vault.connection, f"/{name}")
    except Missing:
        # No such top-level entry (e.g. a parent rolled back by a failed
        # write); there are simply no children under it.
        return 0
    return vault.connection.execute(
        "SELECT COUNT(*) FROM entries WHERE parent_id = ?", (parent["id"],)
    ).fetchone()[0]


def _parent_row(vault: memhub.Vault, name: str) -> sqlite3.Row:
    """Return the stored row for the top-level directory ``name`` (raising if
    it is absent), including its ``created_at``/``updated_at`` stamps."""
    return resolve(vault.connection, f"/{name}")


def _entry_row(vault: memhub.Vault, path: str) -> sqlite3.Row:
    """Return the stored row at ``path`` (raising if it is absent)."""
    return resolve(vault.connection, path)


# --------------------------------------------------------------------------- #
# Records and unconditional writes
# --------------------------------------------------------------------------- #


def test_write_file_returns_write_result_with_entry_and_hash(vault):
    result = write_file(vault, "/hello.txt", "world")
    assert isinstance(result, WriteResult)
    assert isinstance(result.entry, Entry)
    assert result.content_hash == content_hash("world")


def test_write_creates_new_file_record(vault):
    result = write_file(vault, "/note.txt", "body")
    assert result.entry.kind == FILE
    assert result.entry.path == "/note.txt"
    assert isinstance(result.entry.id, int)
    assert result.entry.size_bytes == len("body".encode("utf-8"))
    assert result.entry.created_at
    assert result.entry.updated_at


def test_write_empty_content_is_admitted(vault):
    result = write_file(vault, "/empty.txt", "")
    assert result.entry.size_bytes == 0
    assert result.content_hash == content_hash("")
    stored = _entry_row(vault, "/empty.txt")
    assert stored["content"] == ""


def test_write_overwrites_content_and_updates_updated_at(vault):
    first = write_file(vault, "/f.txt", "one")
    time.sleep(0.01)
    second = write_file(vault, "/f.txt", "two")

    assert _entry_row(vault, "/f.txt")["content"] == "two"
    # Creation time is preserved across the overwrite (identity is immutable).
    assert second.entry.created_at == first.entry.created_at
    # Updated timestamp advances.
    assert second.entry.updated_at >= first.entry.updated_at


def test_write_preserves_creation_time_and_original_bytes(vault):
    created = write_file(vault, "/preserve.txt", "café\n")
    original = _entry_row(vault, "/preserve.txt")
    assert original["content"] == "café\n"
    created_at = original["created_at"]

    overwritten = write_file(vault, "/preserve.txt", "changed")
    after = _entry_row(vault, "/preserve.txt")
    assert after["content"] == "changed"
    assert after["created_at"] == created_at
    assert after["id"] == created.entry.id

    # The written bytes are exactly the original UTF-8 bytes, and the hash covers
    # the complete document.
    assert content_hash("café\n") == created.content_hash
    assert after["content"].encode("utf-8") == b"changed"


def test_write_reports_utf8_byte_size_not_character_count(vault):
    # "héllo" is 5 characters but 6 UTF-8 bytes (é is two bytes).
    result = write_file(vault, "/bytes.txt", "héllo")
    assert result.entry.size_bytes == 6
    assert result.entry.size_bytes == len("héllo".encode("utf-8"))


def test_new_child_bumps_direct_parent_updated_at(vault):
    # Let the parent ``/a`` already exist with a known membership timestamp by
    # writing the first child into it first.
    write_file(vault, "/a/first.txt", "x")
    parent_before = _parent_row(vault, "a")

    time.sleep(0.01)
    write_file(vault, "/a/second.txt", "y")
    parent_after = _parent_row(vault, "a")

    # Creating a new file under an existing directory advances that directly
    # affected parent's updated_at (a direct-child membership change), within
    # the same transaction as the write.
    assert parent_after["updated_at"] > parent_before["updated_at"]
    # The directory's own identity and creation stamp are untouched.
    assert parent_after["created_at"] == parent_before["created_at"]


def test_overwrite_does_not_change_direct_parent_updated_at(vault):
    # Two children already exist, so ``/a`` has a stable membership timestamp.
    write_file(vault, "/a/first.txt", "x")
    write_file(vault, "/a/second.txt", "y")
    parent_before = _parent_row(vault, "a")

    time.sleep(0.01)
    write_file(vault, "/a/second.txt", "changed")
    parent_after = _parent_row(vault, "a")

    # Overwriting an existing file is a content mutation: only the file's own
    # updated_at changes. The directly affected directory's timestamp does not
    # move.
    assert parent_after["updated_at"] == parent_before["updated_at"]
    assert parent_after["created_at"] == parent_before["created_at"]


# --------------------------------------------------------------------------- #
# Directory tree conflicts
# --------------------------------------------------------------------------- #


def test_write_into_existing_directory_is_conflict(vault):
    write_file(vault, "/adir/placeholder.txt", "x")
    with pytest.raises(Conflict) as conflicted:
        write_file(vault, "/adir", "y")
    assert conflicted.value.code == "exists_as_directory"


def test_write_into_missing_directory_creates_parents(vault):
    result = write_file(vault, "/new/deep/document.txt", "nested")
    assert result.entry.kind == FILE
    # Every intermediate component exists as a directory.
    for component in ("/new", "/new/deep"):
        row = vault.connection.execute(
            "SELECT kind FROM entries WHERE name = ?", (component.rsplit("/", 1)[-1],)
        ).fetchone()
        assert row["kind"] == DIRECTORY


# --------------------------------------------------------------------------- #
# Conditional writes: mutually exclusive, absent, match
# --------------------------------------------------------------------------- #


def test_if_match_and_if_absent_are_mutually_exclusive(vault):
    with pytest.raises(Conflict) as conflicted:
        write_file(vault, "/both.txt", "x", if_match="ab", if_absent=True)
    assert conflicted.value.code == "write_conditions_mutually_exclusive"
    # Nothing was written.
    assert _count_under_parent(vault, "both.txt") == 0


def test_if_absent_writes_when_missing_then_rejects_when_present(vault):
    result = write_file(vault, "/absent.txt", "a", if_absent=True)
    assert result.entry.path == "/absent.txt"

    with pytest.raises(Conflict) as conflicted:
        write_file(vault, "/absent.txt", "b", if_absent=True)
    assert conflicted.value.code == "if_absent_exists"
    # The original content survived the rejected write.
    assert _entry_row(vault, "/absent.txt")["content"] == "a"


def test_if_match_accepts_current_hash_and_rejects_stale_hash(vault):
    write_file(vault, "/match.txt", "payload")
    current = content_hash("payload")

    # A correct match is an overwrite.
    matched = write_file(vault, "/match.txt", "payload2", if_match=current)
    assert matched.entry.path == "/match.txt"

    # A stale hash is rejected.
    with pytest.raises(Conflict) as conflicted:
        write_file(vault, "/match.txt", "payload3", if_match="00" * 32)
    assert conflicted.value.code == "stale_hash"
    assert _entry_row(vault, "/match.txt")["content"] == "payload2"


def test_if_match_on_missing_document_is_conflict(vault):
    with pytest.raises(Conflict) as conflicted:
        write_file(vault, "/match/missing.txt", "x", if_match="dead" * 8)
    assert conflicted.value.code == "if_match_missing_document"


# --------------------------------------------------------------------------- #
# Atomic parent rollback
# --------------------------------------------------------------------------- #


def test_failed_if_match_rolls_back_all_created_parents(vault):
    with pytest.raises(Conflict):
        write_file(vault, "/rolled/new/file.txt", "x", if_match="dead" * 8)
    # Every created parent rolled back with the failed transaction.
    assert _count_under_parent(vault, "rolled") == 0


def test_created_parents_roll_back_on_transaction_failure(vault):
    with pytest.raises(RuntimeError):
        with vault.transaction(write=True):
            write_in_transaction(vault.connection, "/rollback/a/b.txt", "x")
            raise RuntimeError("boom")
    assert _count_under_parent(vault, "rollback") == 0
    # The vault remains usable after the rollback.
    assert _count_under_root(vault) == 1


def test_write_in_transaction_uses_caller_owned_transaction(vault):
    with vault.transaction(write=True):
        result = write_in_transaction(vault.connection, "/owned/doc.txt", "data")
    assert result.entry.path == "/owned/doc.txt"
    assert _entry_row(vault, "/owned/doc.txt")["content"] == "data"


# --------------------------------------------------------------------------- #
# Content admission
# --------------------------------------------------------------------------- #


def test_write_rejects_control_character_content(vault):
    with pytest.raises(InvalidInput):
        write_file(vault, "/bad.txt", "line\x01control")


def test_write_normalizes_path_before_resolution(vault):
    result = write_file(vault, "/multi//path//", "x")
    assert result.entry.path == "/multi/path"
    # The input normalizes to the canonical leaf file ``/multi/path`` ...
    assert _entry_row(vault, "/multi/path")["name"] == "path"
    # ... and its parent ``multi`` has exactly one child (that file).
    assert _count_under_parent(vault, "multi") == 1
