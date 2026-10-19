import re
import sqlite3

import pytest

from memhub import store
from memhub.store import (
    Memory,
    MemhubError,
    content_type_for,
    has_descendants,
    init_store,
    is_memory,
    normalize_path,
    open_store,
    put,
    read_content,
    stat,
)


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "s.sqlite"
    init_store(db)
    with open_store(db) as connection:
        yield connection

_INSERT = (
    "INSERT INTO memories (id, path, content, blob, content_type, size, "
    "created_at, updated_at) "
    "VALUES ('x', '/x', 'c', NULL, 'text/plain', 1, "
    "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
)


def test_normalize_path_adds_leading_slash():
    assert normalize_path("a/b") == "/a/b"


def test_normalize_path_strips_trailing_slash():
    assert normalize_path("/a/b/") == "/a/b"


def test_normalize_path_root():
    assert normalize_path("/") == "/"


@pytest.mark.parametrize("bad", ["a//b", "a/../b", "a/./b", ""])
def test_normalize_path_rejects_invalid(bad):
    with pytest.raises(MemhubError):
        normalize_path(bad)


def test_normalize_path_unicode_and_spaces_pass_through():
    assert normalize_path("/notes/été 2026.md") == "/notes/été 2026.md"


def test_init_store_creates_db(tmp_path):
    db = tmp_path / "s.sqlite"
    init_store(db)
    assert db.exists()


def test_init_store_is_idempotent(tmp_path):
    db = tmp_path / "s.sqlite"
    init_store(db)
    init_store(db)  # calling twice does not raise


def test_open_store_checkpoints_wal_on_exit(tmp_path):
    db = tmp_path / "s.sqlite"
    init_store(db)
    with open_store(db) as conn:
        # WAL must actually be enabled on the connection we hand out.
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        # Perform a real write so the -wal/-shm sidecars demonstrably exist
        # mid-context (otherwise the post-exit assertions would pass trivially).
        conn.execute(_INSERT)
        assert db.with_name(db.name + "-wal").exists()
        assert db.with_name(db.name + "-shm").exists()
    assert not db.with_name(db.name + "-wal").exists()
    assert not db.with_name(db.name + "-shm").exists()


def test_open_store_truncates_wal_explicitly(tmp_path):
    """The explicit wal_checkpoint(TRUNCATE) must run, not just close().

    SQLite checkpoints automatically when the *last* connection closes, so the
    sidecar-free state asserted in test_open_store_checkpoints_wal_on_exit can
    be produced by close() alone. Holding a second connection open across the
    context suppresses that implicit checkpoint, which makes the explicit one
    observable: the -wal file survives but must be truncated to 0 bytes.
    """
    db = tmp_path / "s.sqlite"
    init_store(db)
    wal = db.with_name(db.name + "-wal")
    reader = sqlite3.connect(db)
    try:
        reader.execute("PRAGMA journal_mode=WAL")
        reader.execute("SELECT count(*) FROM memories").fetchone()
        with open_store(db) as conn:
            conn.execute(_INSERT)
            assert wal.exists()
            assert db.with_name(db.name + "-shm").exists()
        assert wal.exists(), "lingering reader should keep the WAL file alive"
        assert wal.stat().st_size == 0, "WAL should be truncated on clean exit"
    finally:
        reader.close()
    assert not wal.exists()
    assert not db.with_name(db.name + "-shm").exists()


# --- put / read_content / stat -------------------------------------------------


def test_put_text_round_trip(conn):
    data = "héllo".encode("utf-8")
    m = put(conn, "/notes/a.txt", data)
    assert isinstance(m, Memory)
    assert m.path == "/notes/a.txt"
    assert m.content_type == "text/plain"
    assert m.size == len(data)
    assert re.fullmatch(r"[0-9a-f]{32}", m.id)
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", m.created_at)
    assert m.updated_at == m.created_at
    assert read_content(conn, "/notes/a.txt") == data
    assert stat(conn, "/notes/a.txt") == m
    # Text is stored decoded in `content`, with blob NULL.
    row = conn.execute(
        "SELECT content, blob FROM memories WHERE path = '/notes/a.txt'"
    ).fetchone()
    assert row[0] == "héllo"
    assert row[1] is None


def test_put_binary_round_trip_is_byte_exact(conn):
    data = b"\x00\xff\xfe"
    m = put(conn, "/bin/x", data)
    assert m.content_type == "application/octet-stream"
    assert m.size == 3
    assert read_content(conn, "/bin/x") == data
    # Binary is stored in `blob`, with content NULL.
    row = conn.execute(
        "SELECT content, blob FROM memories WHERE path = '/bin/x'"
    ).fetchone()
    assert row[0] is None
    assert bytes(row[1]) == data


def test_put_overwrite_keeps_id_and_created_at(conn, monkeypatch):
    first = put(conn, "/a", b"one")
    monkeypatch.setattr(store, "_now", lambda: "2027-01-01T00:00:00Z")
    second = put(conn, "/a", b"two")
    assert second.id == first.id
    assert second.created_at == first.created_at
    assert second.updated_at == "2027-01-01T00:00:00Z"
    assert read_content(conn, "/a") == b"two"
    assert conn.execute("SELECT count(*) FROM memories").fetchone()[0] == 1
    assert stat(conn, "/a") == second


def test_delete_and_reput_gets_new_id(conn):
    first = put(conn, "/a", b"x")
    conn.execute("DELETE FROM memories WHERE path = '/a'")
    second = put(conn, "/a", b"x")
    assert second.id != first.id


@pytest.mark.parametrize("path", ["/a", "/a/b"])
def test_put_rejects_when_ancestor_is_a_memory(conn, path):
    put(conn, "/a/b/c", b"x")
    with pytest.raises(MemhubError):
        put(conn, path, b"y")
    assert read_content(conn, "/a/b/c") == b"x"
    assert conn.execute("SELECT count(*) FROM memories").fetchone()[0] == 1


@pytest.mark.parametrize("path", ["/a/b", "/a/b/c"])
def test_put_rejects_when_path_is_ancestor_of_memories(conn, path):
    put(conn, "/a", b"x")
    with pytest.raises(MemhubError):
        put(conn, path, b"y")
    assert read_content(conn, "/a") == b"x"


def test_put_at_root_is_rejected(conn):
    with pytest.raises(MemhubError):
        put(conn, "/", b"x")
    assert conn.execute("SELECT count(*) FROM memories").fetchone()[0] == 0


def test_put_rejects_text_plain_for_invalid_utf8(conn):
    with pytest.raises(MemhubError):
        put(conn, "/bad", b"\xff\xfe", content_type="text/plain")
    assert conn.execute("SELECT count(*) FROM memories WHERE path = '/bad'").fetchone()[0] == 0


def test_stat_missing_path_raises(conn):
    with pytest.raises(MemhubError):
        stat(conn, "/nope")


def test_read_content_missing_path_raises(conn):
    with pytest.raises(MemhubError):
        read_content(conn, "/nope")


def test_content_type_for_sniffs_utf8():
    assert content_type_for(b"") == "text/plain"
    assert content_type_for(b"hello") == "text/plain"
    assert content_type_for("héllo".encode("utf-8")) == "text/plain"
    assert content_type_for(b"\x00\xff") == "application/octet-stream"
    assert content_type_for(b"\xff\xfe") == "application/octet-stream"


def test_explicit_content_type_override_is_honored(conn):
    m = put(conn, "/note.md", b"# hi", content_type="text/markdown")
    assert m.content_type == "text/markdown"
    row = conn.execute(
        "SELECT content, blob FROM memories WHERE path = '/note.md'"
    ).fetchone()
    assert row[0] is None
    assert bytes(row[1]) == b"# hi"
    assert read_content(conn, "/note.md") == b"# hi"
    # Declaring text/plain routes the same bytes through the text column.
    m2 = put(conn, "/note.txt", b"# hi", content_type="text/plain")
    assert m2.content_type == "text/plain"
    row2 = conn.execute(
        "SELECT content, blob FROM memories WHERE path = '/note.txt'"
    ).fetchone()
    assert row2[0] == "# hi"
    assert row2[1] is None


def test_record_operations_normalize_paths(conn):
    m = put(conn, "notes/a.txt", b"hi")
    assert m.path == "/notes/a.txt"
    assert read_content(conn, "/notes/a.txt/") == b"hi"
    assert stat(conn, "notes/a.txt").path == "/notes/a.txt"
    assert is_memory(conn, "/notes/a.txt/") is True
    with pytest.raises(MemhubError):
        put(conn, "notes/../b", b"hi")
    with pytest.raises(MemhubError):
        stat(conn, "")
    with pytest.raises(MemhubError):
        read_content(conn, "//x")


# --- helpers used by Tasks 4 and 6 --------------------------------------------


def test_is_memory(conn):
    put(conn, "/a/b", b"x")
    assert is_memory(conn, "/a/b") is True
    assert is_memory(conn, "/a") is False
    assert is_memory(conn, "/") is False
    assert is_memory(conn, "/nope") is False


def test_has_descendants_is_component_aware(conn):
    put(conn, "/ab", b"x")
    assert has_descendants(conn, "/a") is False
    assert has_descendants(conn, "/ab") is False
    assert has_descendants(conn, "/") is True
    put(conn, "/a/b", b"y")
    assert has_descendants(conn, "/a") is True
    assert has_descendants(conn, "/a/b") is False
    assert has_descendants(conn, "/") is True
