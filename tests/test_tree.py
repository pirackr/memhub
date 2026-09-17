"""Tree-invariant and resolution tests for :mod:`memhub.tree`.

Run with the installed test environment, e.g.::

    .venv/bin/python -m pytest -q tests/test_tree.py

Two families of tests live here:

* Direct-SQL rejection cases insert rows *without* the storage library so the
  schema constraints themselves are shown to fire; a bad write raises
  ``sqlite3.IntegrityError`` from the raw connection.
* Resolution cases exercise :func:`memhub.tree.resolve` and
  :func:`memhub.tree.ensure_directories`, including rollback of created parents.
"""

from __future__ import annotations

import sqlite3

import pytest

import memhub
from memhub.errors import Conflict, Missing
from memhub.tree import ensure_directories, resolve

# SQL that inserts a valid child directly into the entries table.
_INSERT_CHILD = (
    "INSERT INTO entries "
    "(parent_id, name, kind, content, created_at, updated_at) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)


def _insert(vault, *, parent_id, name, kind, content, commit=False):
    """Insert a row directly and optionally commit it to ``vault``."""
    stamp = "2026-09-17T00:00:00+00:00"
    with vault.transaction(write=True):
        vault.connection.execute(
            _INSERT_CHILD,
            (parent_id, name, kind, content, stamp, stamp),
        )
    if commit:
        vault.connection.commit()


def _expect_integrity(vault, sql, params=()):
    """Assert a raw statement is rejected by the schema and clean up."""
    conn = vault.connection
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, params)
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _row_id(vault, name, parent_id=None):
    query = "SELECT id FROM entries WHERE name = ?"
    params = [name]
    if parent_id is not None:
        query += " AND parent_id = ?"
        params.append(parent_id)
    return vault.connection.execute(query, params).fetchone()[0]


# ---------------------------------------------------------------------------
# Direct-SQL rejection: the schema rejects these even without tree.py.
# ---------------------------------------------------------------------------


def test_second_root_is_rejected_by_sibling_index(vault):
    conn = vault.connection
    _expect_integrity(
        vault,
        "INSERT INTO entries "
        "(parent_id, name, kind, content, created_at, updated_at) "
        "VALUES (NULL, 'another', 'directory', NULL, 't', 't')",
    )
    # The root is still the only root after the rejected write.
    count = conn.execute(
        "SELECT COUNT(*) FROM entries WHERE parent_id IS NULL"
    ).fetchone()[0]
    assert count == 1


def test_missing_parent_is_rejected_by_foreign_key(vault):
    _expect_integrity(
        vault,
        _INSERT_CHILD,
        (999, "orphan", "file", "hi", "t", "t"),
    )
    count = vault.connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    assert count == 1  # only the root remains


def test_file_cannot_have_children(vault):
    _insert(vault, parent_id=1, name="doc", kind="file", content="text", commit=True)
    doc_id = _row_id(vault, "doc")
    _expect_integrity(
        vault,
        _INSERT_CHILD,
        (doc_id, "child", "file", "hi", "t", "t"),
    )


def test_duplicate_sibling_is_rejected_by_unique_index(vault):
    _insert(vault, parent_id=1, name="a", kind="directory", content=None, commit=True)
    _expect_integrity(
        vault,
        _INSERT_CHILD,
        (1, "a", "directory", None, "t", "t"),
    )


def test_invalid_kind_is_rejected(vault):
    _expect_integrity(
        vault,
        _INSERT_CHILD,
        (1, "weird", "sprite", None, "t", "t"),
    )


def test_link_kind_is_rejected(vault):
    # V1 excludes symlinks/hard links: kinds are exactly file or directory.
    _expect_integrity(
        vault,
        _INSERT_CHILD,
        (1, "shortcut", "link", None, "t", "t"),
    )


def test_content_on_a_directory_is_rejected(vault):
    _expect_integrity(
        vault,
        _INSERT_CHILD,
        (1, "notes", "directory", "should not be here", "t", "t"),
    )


def test_empty_child_name_is_rejected(vault):
    _expect_integrity(
        vault,
        _INSERT_CHILD,
        (1, "", "directory", None, "t", "t"),
    )


def test_self_parenting_is_rejected(vault):
    # ensure_directories neither starts nor commits a transaction, so the caller
    # must own the write: commit "/loop" so the rejected UPDATE's rollback does
    # not delete the setup row the rest of the test inspects.
    with vault.transaction(write=True):
        ensure_directories(vault.connection, "/loop")
    loop_id = _row_id(vault, "loop")
    conn = vault.connection
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE entries SET parent_id = ? WHERE id = ?", (loop_id, loop_id)
        )
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass
    # The parent pointer is untouched.
    parent = conn.execute(
        "SELECT parent_id FROM entries WHERE id = ?", (loop_id,)
    ).fetchone()[0]
    assert parent == 1


def test_immutable_identity_rejects_changed_id(vault):
    _insert(vault, parent_id=1, name="doc", kind="file", content="text", commit=True)
    doc_id = _row_id(vault, "doc")
    _expect_integrity(
        vault,
        "UPDATE entries SET id = 9999 WHERE id = ?",
        (doc_id,),
    )


def test_immutable_identity_rejects_changed_name_and_kind(vault):
    _insert(vault, parent_id=1, name="doc", kind="file", content="text", commit=True)
    doc_id = _row_id(vault, "doc")
    _expect_integrity(
        vault,
        "UPDATE entries SET name = 'other' WHERE id = ?",
        (doc_id,),
    )
    _expect_integrity(
        vault,
        "UPDATE entries SET kind = 'directory' WHERE id = ?",
        (doc_id,),
    )


def test_immutable_identity_rejects_changed_parent(vault):
    # Own the write so the later ``_insert(commit=True)`` can take BEGIN IMMEDIATE
    # instead of colliding with the implicit transaction ensure_directories opened.
    with vault.transaction(write=True):
        ensure_directories(vault.connection, "/home")
    home_id = _row_id(vault, "home")
    _insert(vault, parent_id=1, name="doc", kind="file", content="text", commit=True)
    doc_id = _row_id(vault, "doc")
    _expect_integrity(
        vault,
        "UPDATE entries SET parent_id = ? WHERE id = ?",
        (home_id, doc_id),
    )


def test_root_cannot_be_deleted(vault):
    conn = vault.connection
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM entries WHERE parent_id IS NULL")
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass
    count = conn.execute(
        "SELECT COUNT(*) FROM entries WHERE parent_id IS NULL"
    ).fetchone()[0]
    assert count == 1


def test_updated_at_still_changes_without_breaking_identity(vault):
    # Sanity: identity is immutable, but updated_at is deliberately writable.
    _insert(vault, parent_id=1, name="doc", kind="file", content="text", commit=True)
    doc_id = _row_id(vault, "doc")
    vault.connection.execute(
        "UPDATE entries SET updated_at = 'later' WHERE id = ?", (doc_id,)
    )
    vault.connection.commit()
    stamp = vault.connection.execute(
        "SELECT updated_at FROM entries WHERE id = ?", (doc_id,)
    ).fetchone()[0]
    assert stamp == "later"


# ---------------------------------------------------------------------------
# Resolution.
# ---------------------------------------------------------------------------


def test_resolve_root_returns_the_root_row(vault):
    row = resolve(vault.connection, "/")
    assert isinstance(row, sqlite3.Row)
    assert row["id"] == 1
    assert row["name"] == ""
    assert row["kind"] == "directory"


def test_resolve_missing_path_raises_missing(vault):
    with pytest.raises(Missing) as missing:
        resolve(vault.connection, "/does-not-exist")
    assert missing.value.code == "no_such_path"


def test_resolve_nested_components(vault):
    ensure_directories(vault.connection, "/a/b/c")
    deep = resolve(vault.connection, "/a/b/c")
    assert deep["name"] == "c"
    assert deep["kind"] == "directory"
    assert resolve(vault.connection, "/a")["name"] == "a"
    assert resolve(vault.connection, "/a/b")["name"] == "b"


def test_resolve_hidden_names(vault):
    ensure_directories(vault.connection, "/.hidden/.deeper")
    hidden = resolve(vault.connection, "/.hidden")
    assert hidden["name"] == ".hidden"
    assert resolve(vault.connection, "/.hidden/.deeper")["name"] == ".deeper"


def test_resolve_directory_resolves_on_its_own(vault):
    ensure_directories(vault.connection, "/a")
    assert resolve(vault.connection, "/a")["name"] == "a"


def test_resolve_descending_through_file_is_conflict(vault):
    with vault.transaction(write=True):
        ensure_directories(vault.connection, "/a")
    _insert(vault, parent_id=_row_id(vault, "a"), name="f", kind="file",
            content="x", commit=True)
    with pytest.raises(Conflict) as conflict:
        resolve(vault.connection, "/a/f/g")
    assert conflict.value.code == "path_component_not_directory"


# ---------------------------------------------------------------------------
# ensure_directories.
# ---------------------------------------------------------------------------


def test_ensure_directories_creates_a_nested_tree(vault):
    row = ensure_directories(vault.connection, "/a/b/c")
    assert row["name"] == "c"
    # resolve returns the final component's basename, so compare against that
    # rather than ``component.strip('/')`` (which keeps "/a/b" -> "a/b").
    for component in ("/a", "/a/b", "/a/b/c"):
        assert resolve(vault.connection, component)["name"] == component.rsplit("/", 1)[-1]


def test_ensure_directories_is_idempotent(vault):
    first = ensure_directories(vault.connection, "/a")
    again = ensure_directories(vault.connection, "/a")
    assert first["id"] == again["id"]
    count = vault.connection.execute(
        "SELECT COUNT(*) FROM entries WHERE name = 'a'"
    ).fetchone()[0]
    assert count == 1


def test_ensure_directories_reuses_existing_directory(vault):
    ensure_directories(vault.connection, "/a")
    child = ensure_directories(vault.connection, "/a/b")
    assert child["name"] == "b"
    assert child["parent_id"] == _row_id(vault, "a")


def test_ensure_directories_root_returns_root(vault):
    row = ensure_directories(vault.connection, "/")
    assert row["id"] == 1
    assert row["kind"] == "directory"


def test_ensure_directories_rejects_descending_into_file(vault):
    with vault.transaction(write=True):
        ensure_directories(vault.connection, "/a")
    _insert(vault, parent_id=_row_id(vault, "a"), name="f", kind="file",
            content="x", commit=True)
    with pytest.raises(Conflict) as conflict:
        ensure_directories(vault.connection, "/a/f/g")
    assert conflict.value.code == "path_component_not_directory"


def test_ensure_directories_creates_utc_timestamps(vault):
    row = ensure_directories(vault.connection, "/stamped")
    assert row["created_at"] == row["updated_at"]
    assert row["created_at"].endswith("+00:00")
    assert row["kind"] == "directory"
    assert row["content"] is None


def test_created_parents_roll_back_on_failed_transaction(vault):
    with pytest.raises(RuntimeError):
        with vault.transaction(write=True):
            ensure_directories(vault.connection, "/a/b/c")
            raise RuntimeError("abort the batch")

    # Nothing survived the rollback.
    for component in ("/a", "/a/b", "/a/b/c"):
        with pytest.raises(Missing):
            resolve(vault.connection, component)
    count = vault.connection.execute(
        "SELECT COUNT(*) FROM entries WHERE name = 'a'"
    ).fetchone()[0]
    assert count == 0  # the failed transaction rolled back every created parent


def test_resolve_and_ensure_return_sqlite_rows(vault):
    # They simply read/write the passed connection and hand back sqlite3.Row.
    root = resolve(vault.connection, "/")
    assert isinstance(root, sqlite3.Row)
    created = ensure_directories(vault.connection, "/flat")
    assert isinstance(created, sqlite3.Row)
    assert created["name"] == "flat"


def test_tree_invariants_leave_database_intact(vault):
    ensure_directories(vault.connection, "/x/y/z")
    result = vault.connection.execute("PRAGMA integrity_check").fetchone()[0]
    assert result == "ok"


def test_created_parents_rollback_leaves_integrity_ok(vault):
    with pytest.raises(RuntimeError):
        with vault.transaction(write=True):
            ensure_directories(vault.connection, "/rollback/a")
            raise RuntimeError("abort")
    assert vault.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
