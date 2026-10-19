import sqlite3

import pytest

from memhub.store import MemhubError, init_store, normalize_path, open_store

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
