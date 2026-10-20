"""reindex: v1 -> v2 migration, blob backfill, index rebuild."""

import sqlite3
import threading
import time

import pytest

from memhub import store
from memhub.store import (
    MemhubError,
    ReindexReport,
    init_store,
    mv,
    open_store,
    put,
    read_content,
    reindex,
    rm,
    schema_version,
)

_MD = b"# Widget\n\nnotes about widgets\n"
_BAD = b"\xff\xfe damaged secretword \x80"
_BIN = b"\x00\x01\x02\x03png\xff\xfe"

_V1_INSERT = (
    "INSERT INTO memories (id, path, content, blob, content_type, size, "
    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)


def _fts_paths(conn, query):
    return sorted(
        row[0]
        for row in conn.execute(
            "SELECT path FROM memories_fts WHERE memories_fts MATCH ?", (query,)
        ).fetchall()
    )


def _vocab(conn):
    """Term-level postings read straight out of the FTS index.

    fts5vocab scans the index itself, so -- unlike a plain SELECT on the
    external-content table, which enumerates live `memories` rows and resolves
    path/content from them -- it reflects what is actually indexed and can
    detect a stale or corrupt one. 'col' mode is one row per (term, column)
    with doc = documents containing the term and cnt = total occurrences (the
    successor of the old 'term' mode's hits/phits columns; current SQLite
    accepts 'row'/'col'/'instance', not 'rowid'/'term').
    """
    conn.execute("DROP TABLE IF EXISTS temp.vocab")
    conn.execute(
        "CREATE VIRTUAL TABLE temp.vocab USING fts5vocab(main, memories_fts, col)"
    )
    rows = conn.execute(
        "SELECT term, col, doc, cnt FROM temp.vocab ORDER BY term, col"
    ).fetchall()
    conn.execute("DROP TABLE temp.vocab")
    return rows


def _objects(conn):
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE name IS NOT NULL"
        ).fetchall()
    }


def _v1_db(tmp_path, version=1):
    """A layer-1 store file: memories table only, user_version = version."""
    db = tmp_path / "s.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(store._SCHEMA)
    conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()
    conn.close()
    return db


def _seed_v1(db):
    """Pre-Task-1 data: text rows carry their bytes in blob, not content."""
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            _V1_INSERT,
            (
                "a" * 32,
                "/notes/widget.md",
                None,
                _MD,
                "text/markdown",
                len(_MD),
                "2026-01-01T00:00:00Z",
                "2026-01-02T00:00:00Z",
            ),
        )
        conn.execute(
            _V1_INSERT,
            (
                "b" * 32,
                "/legacy/damaged.txt",
                None,
                _BAD,
                "text/plain",
                len(_BAD),
                "2026-01-03T00:00:00Z",
                "2026-01-03T00:00:00Z",
            ),
        )
        conn.execute(
            _V1_INSERT,
            (
                "c" * 32,
                "/img/logo.png",
                None,
                _BIN,
                "image/png",
                len(_BIN),
                "2026-01-04T00:00:00Z",
                "2026-01-04T00:00:00Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _row(conn, path):
    return conn.execute(
        "SELECT id, content, blob, content_type, size, created_at, updated_at "
        "FROM memories WHERE path = ?",
        (path,),
    ).fetchone()


# --- v1 upgrade: schema + backfill + counts ---------------------------------


def test_reindex_upgrades_v1_store(tmp_path):
    db = _v1_db(tmp_path)
    _seed_v1(db)
    conn = sqlite3.connect(db)
    try:
        report = reindex(conn)
        assert report == ReindexReport(
            upgraded=True, indexed=3, backfilled=1, invalid_utf8=1
        )
        assert schema_version(conn) == 2
        assert {
            "memories_fts",
            "memories_ai",
            "memories_ad",
            "memories_au",
        } <= _objects(conn)
        # the backfilled markdown row is now searchable by a content word
        assert _fts_paths(conn, "widget") == ["/notes/widget.md"]
    finally:
        conn.close()


def test_reindex_backfill_preserves_bytes_and_metadata(tmp_path):
    db = _v1_db(tmp_path)
    _seed_v1(db)
    conn = sqlite3.connect(db)
    try:
        before = {p: _row(conn, p) for p in ("/notes/widget.md", "/legacy/damaged.txt")}
        reindex(conn)
        after = {p: _row(conn, p) for p in before}
        for path in before:
            _, _, _, ctype, size, created, updated = before[path]
            assert after[path][0] == before[path][0]  # id
            assert after[path][3:] == (ctype, size, created, updated)
        # text row: bytes moved blob -> content, byte-exact
        assert read_content(conn, "/notes/widget.md") == _MD
        assert _row(conn, "/notes/widget.md")[1] == _MD.decode("utf-8")
        assert _row(conn, "/notes/widget.md")[2] is None
        # invalid-UTF-8 text row: untouched, still blob-only, byte-exact
        assert _row(conn, "/legacy/damaged.txt")[1] is None
        assert bytes(_row(conn, "/legacy/damaged.txt")[2]) == _BAD
        assert read_content(conn, "/legacy/damaged.txt") == _BAD
        # binary row untouched
        assert read_content(conn, "/img/logo.png") == _BIN
    finally:
        conn.close()


def test_reindex_invalid_utf8_row_is_indexed_by_path_only(tmp_path):
    db = _v1_db(tmp_path)
    _seed_v1(db)
    conn = sqlite3.connect(db)
    try:
        reindex(conn)
        assert _fts_paths(conn, "damaged") == ["/legacy/damaged.txt"]
        assert _fts_paths(conn, "secretword") == []
    finally:
        conn.close()


def test_reindex_upgrades_unversioned_store(tmp_path):
    """user_version = 0 (pre-versioning store) is upgraded like v1."""
    db = _v1_db(tmp_path, version=0)
    _seed_v1(db)
    conn = sqlite3.connect(db)
    try:
        report = reindex(conn)
        assert report.upgraded is True
        assert report.backfilled == 1
        assert schema_version(conn) == 2
        assert _fts_paths(conn, "widget") == ["/notes/widget.md"]
    finally:
        conn.close()


def test_reindex_is_idempotent_on_v1_counts(tmp_path):
    """A second run finds nothing left to backfill."""
    db = _v1_db(tmp_path)
    _seed_v1(db)
    conn = sqlite3.connect(db)
    try:
        reindex(conn)
        report = reindex(conn)
        assert report == ReindexReport(
            upgraded=False, indexed=3, backfilled=0, invalid_utf8=0
        )
        assert schema_version(conn) == 2
    finally:
        conn.close()


# --- v2 rebuild --------------------------------------------------------------


def test_reindex_v2_matches_trigger_maintained_index(tmp_path):
    db = tmp_path / "s.sqlite"
    init_store(db)
    with open_store(db) as conn:
        put(conn, "/a/one.md", b"alpha widget")
        put(conn, "/a/two.md", b"beta gadget")
        put(
            conn,
            "/b/three.bin",
            b"\xff\x00bin",
            content_type="application/octet-stream",
        )
        mv(conn, "/a/two.md", "/a/renamed.md")
        rm(conn, "/b/three.bin")
        # The control has to be the index itself: `SELECT rowid, path, content
        # FROM memories_fts` enumerates the live memories rows and would match
        # before and after no matter how broken the index is.
        before = _vocab(conn)
        assert before, "empty vocab would make the comparison vacuous"
        report = reindex(conn)
        assert report == ReindexReport(
            upgraded=False, indexed=2, backfilled=0, invalid_utf8=0
        )
        assert _vocab(conn) == before
        # each term resolves to exactly one row
        assert _fts_paths(conn, "widget") == ["/a/one.md"]
        assert _fts_paths(conn, "gadget") == ["/a/renamed.md"]
        assert _fts_paths(conn, "renamed") == ["/a/renamed.md"]


def test_reindex_twice_on_v2_is_stable(tmp_path):
    db = tmp_path / "s.sqlite"
    init_store(db)
    with open_store(db) as conn:
        put(conn, "/a.md", b"alpha widget")
        put(conn, "/b.md", b"beta gadget")
        first = reindex(conn)
        second = reindex(conn)
        assert first == second
        assert first.upgraded is False
        assert first.indexed == 2
        assert _fts_paths(conn, "widget") == ["/a.md"]


def test_reindex_repairs_drifted_index(tmp_path):
    """The index is derived and disposable: rebuild repairs drift."""
    db = tmp_path / "s.sqlite"
    init_store(db)
    put_row = (
        "INSERT INTO memories (id, path, content, blob, content_type, size, "
        "created_at, updated_at) VALUES (?, ?, ?, NULL, ?, ?, ?, ?)"
    )
    conn = sqlite3.connect(db)
    try:
        conn.execute("DROP TRIGGER memories_ai")  # simulate a lost trigger
        conn.execute("DROP TRIGGER memories_ad")
        conn.execute("DROP TRIGGER memories_au")
        conn.execute(
            put_row,
            ("d" * 32, "/raw.md", "orphanword", "text/plain", 10, "x", "y"),
        )
        conn.commit()
        orphan = ("orphanword", "content", 1, 1)
        assert orphan not in _vocab(conn)  # drifted: absent from the index
        assert _fts_paths(conn, "orphanword") == []
        report = reindex(conn)
        assert report.upgraded is False
        assert report.indexed == 1
        assert orphan in _vocab(conn)  # rebuilt from memories
        assert _fts_paths(conn, "orphanword") == ["/raw.md"]
    finally:
        conn.close()


# --- error paths -------------------------------------------------------------


def test_reindex_creates_missing_index_on_v2_store(tmp_path):
    """A v2 store with no index at all is rebuilt, not re-upgraded."""
    db = tmp_path / "s.sqlite"
    init_store(db)
    with open_store(db) as conn:
        put(conn, "/a.md", b"alpha widget")
    conn = sqlite3.connect(db)
    try:
        conn.execute("DROP TABLE memories_fts")
        for trigger in ("memories_ai", "memories_ad", "memories_au"):
            conn.execute(f"DROP TRIGGER {trigger}")
        conn.commit()
        report = reindex(conn)
        assert report == ReindexReport(
            upgraded=False, indexed=1, backfilled=0, invalid_utf8=0
        )
        assert {"memories_fts", "memories_ai", "memories_ad", "memories_au"} <= _objects(
            conn
        )
        assert _fts_paths(conn, "widget") == ["/a.md"]
    finally:
        conn.close()


def test_reindex_through_open_store_on_v1_store(tmp_path):
    """The CLI path: open_store on a v1 file, reindex, commit, then use it."""
    db = _v1_db(tmp_path)
    _seed_v1(db)
    with open_store(db) as conn:
        assert schema_version(conn) == 1
        report = reindex(conn)
        assert report.upgraded is True
        put(conn, "/new/note.md", b"fresh gadget")
    conn = sqlite3.connect(db)
    try:
        assert schema_version(conn) == 2
        assert _fts_paths(conn, "widget") == ["/notes/widget.md"]
        # the recreated triggers are live for later writes
        assert _fts_paths(conn, "gadget") == ["/new/note.md"]
    finally:
        conn.close()


def test_reindex_rejects_newer_store(tmp_path):
    db = _v1_db(tmp_path, version=store._SCHEMA_VERSION + 1)
    conn = sqlite3.connect(db)
    try:
        with pytest.raises(
            MemhubError, match="store was created by a newer memhub version"
        ):
            reindex(conn)
        assert schema_version(conn) == store._SCHEMA_VERSION + 1
    finally:
        conn.close()


def test_reindex_rejects_file_without_memories_table(tmp_path):
    db = tmp_path / "other.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE something_else (x)")
    conn.commit()
    conn.close()
    conn = sqlite3.connect(db)
    try:
        with pytest.raises(MemhubError, match="not a memhub store"):
            reindex(conn)
    finally:
        conn.close()


# --- atomicity and concurrency ----------------------------------------------


def test_reindex_failure_rolls_back_completely(tmp_path, monkeypatch):
    db = _v1_db(tmp_path)
    _seed_v1(db)

    def boom(conn):
        raise MemhubError("interrupted")

    monkeypatch.setattr(store, "_rebuild_index", boom)
    conn = sqlite3.connect(db)
    try:
        with pytest.raises(MemhubError, match="interrupted"):
            reindex(conn)
    finally:
        conn.close()

    conn = sqlite3.connect(db)
    try:
        assert schema_version(conn) == 1
        assert "memories_fts" not in _objects(conn)
        assert "memories_ai" not in _objects(conn)
        # the backfill is rolled back too: bytes still sit in blob
        row = _row(conn, "/notes/widget.md")
        assert row[1] is None
        assert bytes(row[2]) == _MD
    finally:
        conn.close()


def test_concurrent_reindex_loser_rechecks_version(tmp_path, monkeypatch):
    """A blocked reindex re-checks the version and no-ops the upgrade."""
    db = _v1_db(tmp_path)
    _seed_v1(db)
    started = threading.Event()
    release = threading.Event()
    real = store._rebuild_index
    outcome = {}

    def slow_rebuild(connection):
        started.set()
        assert release.wait(timeout=10), "release never set"
        return real(connection)

    def worker(name, grace=0.0):
        time.sleep(grace)
        conn = sqlite3.connect(db, timeout=30)
        try:
            outcome[name] = reindex(conn)
        except BaseException as exc:  # pragma: no cover - surfaced below
            outcome[name + "_error"] = exc
        finally:
            conn.close()

    monkeypatch.setattr(store, "_rebuild_index", slow_rebuild)
    winner = threading.Thread(target=worker, args=("winner",))
    winner.start()
    assert started.wait(timeout=10), "first reindex never built the index"
    # The loser queues behind the write lock (busy timeout 30s), so it acquires
    # the lock only after the winner commits -- and must then see version 2.
    loser = threading.Thread(target=worker, args=("loser", 0.3))
    loser.start()
    time.sleep(0.3)
    release.set()
    winner.join(timeout=20)
    loser.join(timeout=20)
    assert not winner.is_alive() and not loser.is_alive()
    assert "winner_error" not in outcome and "loser_error" not in outcome
    assert outcome["winner"].upgraded is True
    assert outcome["loser"] == ReindexReport(
        upgraded=False, indexed=3, backfilled=0, invalid_utf8=0
    )
    conn = sqlite3.connect(db)
    try:
        assert schema_version(conn) == 2
        assert _fts_paths(conn, "widget") == ["/notes/widget.md"]
    finally:
        conn.close()


def test_concurrent_reindex_serializes(tmp_path, monkeypatch):
    """While one reindex holds the write lock, a second fails cleanly."""
    db = _v1_db(tmp_path)
    _seed_v1(db)
    started = threading.Event()
    release = threading.Event()
    real = store._rebuild_index
    outcome = {}

    def slow_rebuild(connection):
        started.set()
        assert release.wait(timeout=10), "release never set"
        return real(connection)

    def worker():
        conn = sqlite3.connect(db)
        try:
            outcome["report"] = reindex(conn)
        except BaseException as exc:  # pragma: no cover - surfaced below
            outcome["error"] = exc
        finally:
            conn.close()

    monkeypatch.setattr(store, "_rebuild_index", slow_rebuild)
    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert started.wait(timeout=10), "first reindex never built the index"
        loser = sqlite3.connect(db, timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError):
                reindex(loser)
        finally:
            loser.close()
    finally:
        release.set()
        thread.join(timeout=20)

    assert not thread.is_alive()
    assert "error" not in outcome
    assert outcome["report"].upgraded is True
    conn = sqlite3.connect(db)
    try:
        assert schema_version(conn) == 2
        assert _fts_paths(conn, "widget") == ["/notes/widget.md"]
    finally:
        conn.close()
