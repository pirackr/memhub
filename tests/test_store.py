from memhub.store import init_store, open_store


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
        conn.execute("PRAGMA journal_mode")  # touch the store
    assert not db.with_name(db.name + "-wal").exists()
    assert not db.with_name(db.name + "-shm").exists()
