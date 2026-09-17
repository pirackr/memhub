"""Lifecycle tests for the vault: creation, validation, opening, transactions.

Run with the installed test environment, e.g.::

    .venv/bin/python -m pytest -q tests/test_vault.py

These tests only import :mod:`memhub` and its typed errors, so they never pull
the encoding detector.
"""

from __future__ import annotations

import importlib.resources
import sqlite3

import pytest

import memhub
from memhub.errors import (
    Busy,
    Conflict,
    InvalidInput,
    Missing,
    VaultFailure,
)

# 0x4D454D48 as a decimal literal for the SQL pragma in the version test.
_MEMHUB_APP_ID_DECIMAL = 0x4D454D48


def test_create_vault_exclusively_creates_host_file(host_dir):
    vault = host_dir / "vault.db"
    assert not vault.exists()

    result = memhub.create_vault(vault)
    assert result is None
    assert vault.is_file()

    # A second creation over an existing file is rejected.
    with pytest.raises(Conflict) as conflicted:
        memhub.create_vault(vault)
    assert conflicted.value.code == "vault_exists"
    assert conflicted.value.exit_status == 4


def test_create_vault_requires_host_parent(host_dir):
    vault = host_dir / "does-not-exist" / "vault.db"

    with pytest.raises(InvalidInput) as bad:
        memhub.create_vault(vault)
    assert bad.value.code == "host_parent_missing"
    assert bad.value.exit_status == 2
    # Nothing was written: no parent, no file.
    assert not vault.exists()


def test_create_vault_creates_single_root(host_dir):
    vault = host_dir / "vault.db"
    memhub.create_vault(vault)

    with memhub.open_vault(vault) as open_vault:
        root_count = open_vault.connection.execute(
            "SELECT COUNT(*) FROM entries WHERE parent_id IS NULL"
        ).fetchone()[0]
        assert root_count == 1
        root = open_vault.connection.execute(
            "SELECT name, kind, content FROM entries WHERE parent_id IS NULL"
        ).fetchone()
        assert root["name"] == ""
        assert root["kind"] == "directory"
        assert root["content"] is None
        # Exactly one root and a unique sibling index exist in the schema.
        indexes = {
            row[0]
            for row in open_vault.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        assert "entries_sibling_unique" in indexes
        assert "entries_root_unique" in indexes


def test_open_vault_reopens_existing(host_dir):
    vault = host_dir / "vault.db"
    memhub.create_vault(vault)

    opened = memhub.open_vault(vault)
    try:
        assert opened.path == vault
        assert opened.connection is not None
        with opened.transaction():
            assert opened.connection.execute("SELECT 1").fetchone()[0] == 1
    finally:
        opened.close()

    # Closing an already-closed vault is safe.
    opened.close()
    # And it can be reopened again.
    with memhub.open_vault(vault) as reopened:
        assert reopened.connection is not None


def test_open_vault_missing_does_not_create_a_file(host_dir):
    vault = host_dir / "absent.db"

    with pytest.raises(Missing) as missing:
        memhub.open_vault(vault)
    assert missing.value.code == "vault_missing"
    assert missing.value.exit_status == 3
    assert not vault.exists()


def test_open_vault_rejects_unrelated_database(host_dir):
    other = host_dir / "other.db"
    conn = sqlite3.connect(str(other))
    conn.execute("CREATE TABLE other (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    with pytest.raises(VaultFailure) as bad:
        memhub.open_vault(other)
    assert bad.value.code == "not_a_memhub_vault"
    assert bad.value.exit_status == 7


def test_open_vault_rejects_unsupported_version(host_dir):
    upgraded = host_dir / "upgraded.db"
    conn = sqlite3.connect(str(upgraded))
    conn.execute(f"PRAGMA application_id = {_MEMHUB_APP_ID_DECIMAL}")
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()

    with pytest.raises(VaultFailure) as bad:
        memhub.open_vault(upgraded)
    assert bad.value.code == "unsupported_vault_version"
    assert bad.value.exit_status == 7


def test_open_vault_rejects_non_sqlite_host_file(host_dir):
    not_sqlite = host_dir / "notes.txt"
    not_sqlite.write_text("not a database", encoding="utf-8")

    with pytest.raises(VaultFailure):
        memhub.open_vault(not_sqlite)


def test_vault_application_id_and_user_version(host_dir):
    vault = host_dir / "vault.db"
    memhub.create_vault(vault)

    with memhub.open_vault(vault) as opened:
        assert opened.connection.execute("PRAGMA application_id").fetchone()[0] == 0x4D454D48
        assert opened.connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_connection_enables_documented_pragmas(host_dir):
    vault = host_dir / "vault.db"
    memhub.create_vault(vault)

    with memhub.open_vault(vault) as opened:
        conn = opened.connection
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].upper() == "DELETE"
        # synchronous = 2 is FULL.
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2


def test_transaction_yields_the_connection(host_dir):
    vault = host_dir / "vault.db"
    memhub.create_vault(vault)

    with memhub.open_vault(vault) as opened:
        with opened.transaction() as conn:
            assert conn is opened.connection
            conn.execute("CREATE TEMP TABLE scratch (value TEXT)")
            conn.execute("INSERT INTO scratch (value) VALUES ('ok')")
        assert opened.connection.execute(
            "SELECT value FROM scratch"
        ).fetchone()[0] == "ok"


def test_rows_use_sqlite_row_factory(host_dir):
    vault = host_dir / "vault.db"
    memhub.create_vault(vault)

    with memhub.open_vault(vault) as opened:
        row = opened.connection.execute(
            "SELECT id, kind FROM entries WHERE parent_id IS NULL"
        ).fetchone()
        assert isinstance(row, sqlite3.Row)
        assert row["kind"] == "directory"
        assert row[0] == 1


def test_write_transaction_rolls_back_on_raise(host_dir):
    vault = host_dir / "vault.db"
    memhub.create_vault(vault)

    with memhub.open_vault(vault) as opened:
        with pytest.raises(ValueError):
            with opened.transaction(write=True):
                opened.connection.execute(
                    "INSERT INTO entries "
                    "(parent_id, name, kind, content, created_at, updated_at) "
                    "VALUES (1, 'x', 'file', 'hi', 't', 't')"
                )
                raise ValueError("boom")

        # The failed row was rolled back; the vault is still usable.
        with opened.transaction():
            leaf_count = opened.connection.execute(
                "SELECT COUNT(*) FROM entries WHERE parent_id = 1"
            ).fetchone()[0]
            assert leaf_count == 0
            opened.connection.execute("SELECT 1")


def test_busy_timeout_classifies_lock_as_busy(host_dir):
    vault = host_dir / "vault.db"
    memhub.create_vault(vault)

    first = memhub.open_vault(vault)
    second = memhub.open_vault(vault)
    try:
        # Hold a write reservation on one connection, then try to grab it on
        # another; the second writer must get a typed Busy, not a raw error.
        with first.transaction(write=True):
            first.connection.execute(
                "INSERT INTO entries "
                "(parent_id, name, kind, content, created_at, updated_at) "
                "VALUES (1, 'held', 'file', 'x', 't', 't')"
            )
            with pytest.raises(Busy) as busy:
                with second.transaction(write=True):
                    pass
            assert busy.value.exit_status == 6
    finally:
        first.close()
        second.close()


def test_schema_sql_is_a_packaged_resource():
    text = (
        importlib.resources.files("memhub")
        .joinpath("schema.sql")
        .read_text(encoding="utf-8")
    )
    assert "entries" in text
    assert "CREATE TABLE" in text
    assert "CREATE UNIQUE INDEX" in text
