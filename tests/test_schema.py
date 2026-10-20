"""Schema v2 tests: FTS5 table, sync triggers, version checks."""

import sqlite3
from contextlib import closing

import pytest

from memhub import store
from memhub.store import (
    MemhubError,
    init_store,
    mv,
    open_store,
    put,
    rm,
    schema_version,
)


def _fts_paths(conn, query):
    return [
        row[0]
        for row in conn.execute(
            "SELECT path FROM memories_fts WHERE memories_fts MATCH ?", (query,)
        ).fetchall()
    ]


def _objects(db):
    conn = sqlite3.connect(db)
    try:
        return {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE name IS NOT NULL"
            ).fetchall()
        }
    finally:
        conn.close()


# --- fresh init_store: v2 objects ------------------------------------------


def test_init_store_fresh_is_version_2(tmp_path):
    db = tmp_path / "s.sqlite"
    init_store(db)
    conn = sqlite3.connect(db)
    try:
        assert store.schema_version(conn) == 2
        assert store._SCHEMA_VERSION == 2
    finally:
        conn.close()


def test_init_store_creates_fts_table_and_triggers(tmp_path):
    db = tmp_path / "s.sqlite"
    init_store(db)
    names = _objects(db)
    assert {"memories_fts", "memories_ai", "memories_ad", "memories_au"} <= names


def test_init_store_is_idempotent_with_fts(tmp_path):
    db = tmp_path / "s.sqlite"
    init_store(db)
    init_store(db)  # must not raise
    assert "memories_fts" in _objects(db)


# --- trigger sync through the store API -------------------------------------


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "s.sqlite"
    init_store(db)
    with open_store(db) as connection:
        yield connection


def test_fts_indexes_text_put(conn):
    put(conn, "/notes/widget.md", b"a widget spec")
    assert _fts_paths(conn, "widget") == ["/notes/widget.md"]


def test_fts_overwrite_replaces_terms(conn):
    put(conn, "/notes/a.md", b"widget thing")
    put(conn, "/notes/a.md", b"gadget thing")
    assert _fts_paths(conn, "widget") == []
    assert _fts_paths(conn, "gadget") == ["/notes/a.md"]


def test_fts_mv_moves_index_entry(conn):
    put(conn, "/a.md", b"widget")
    mv(conn, "/a.md", "/b.md")
    assert _fts_paths(conn, "widget") == ["/b.md"]
    # Token-level checks: memories_fts is an external-content table, so its
    # `path` column is resolved from the live row and would read '/b.md' even
    # with the UPDATE trigger dropped. The indexed *tokens* are what the trigger
    # maintains: '/a.md' tokenizes to ('a', 'md'), '/b.md' to ('b', 'md').
    assert _fts_paths(conn, "a") == []
    assert _fts_paths(conn, "b") == ["/b.md"]


def test_fts_rm_removes_index_entry(conn):
    put(conn, "/a.md", b"widget")
    rm(conn, "/a.md")
    assert _fts_paths(conn, "widget") == []


def test_fts_rm_recursive_removes_subtree(conn):
    put(conn, "/docs/a.md", b"alpha")
    put(conn, "/docs/b.md", b"beta")
    put(conn, "/docs/sub/c.md", b"gamma")
    put(conn, "/keep.md", b"delta")
    assert rm(conn, "/docs", recursive=True) == 3
    assert _fts_paths(conn, "alpha OR beta OR gamma") == []
    assert _fts_paths(conn, "delta") == ["/keep.md"]


def test_fts_binary_row_indexed_by_path_only(conn):
    blob = b"\x89PNG\xff\xfe\x00secretword"
    put(conn, "/docs/report.pdf", blob, content_type="application/pdf")
    hits = _fts_paths(conn, "pdf")
    assert hits == ["/docs/report.pdf"]
    assert _fts_paths(conn, "secretword") == []


# --- version checks ----------------------------------------------------------


def test_open_store_opens_v1_store_without_migration(tmp_path):
    db = tmp_path / "s.sqlite"
    conn = sqlite3.connect(db)
    try:
        conn.executescript(store._SCHEMA)
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
    finally:
        conn.close()
    with open_store(db) as connection:
        assert schema_version(connection) == 1
    # no implicit migration: still v1, no FTS objects
    with closing(sqlite3.connect(db)) as check:
        assert schema_version(check) == 1
    assert "memories_fts" not in _objects(db)


def test_open_store_rejects_newer_version(tmp_path):
    db = tmp_path / "s.sqlite"
    init_store(db)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    conn.close()
    with pytest.raises(MemhubError, match="store was created by a newer memhub version"):
        with open_store(db):
            pass
