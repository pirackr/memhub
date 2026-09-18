import sqlite3, time
import pytest
from memhub import create_vault, open_vault, write_file, read_file
from memhub.errors import Busy, Conflict


def test_two_writers_same_hash_only_one_wins(tmp_path):
    p=tmp_path/'v'; create_vault(p)
    with open_vault(p) as a, open_vault(p) as b:
        original=write_file(a,'/x','old').content_hash
        write_file(a,'/x','one',if_match=original)
        with pytest.raises(Conflict): write_file(b,'/x','two',if_match=original)
        assert read_file(b,'/x').content=='one'


def test_busy_timeout_maps_to_busy(tmp_path):
    p=tmp_path/'v'; create_vault(p)
    with open_vault(p) as a, open_vault(p) as b:
        a.connection.execute('BEGIN IMMEDIATE')
        start=time.monotonic()
        try:
            with pytest.raises(Busy): write_file(b,'/x','x')
        finally: a.connection.rollback()
        assert 4 <= time.monotonic()-start < 8


def test_uncommitted_write_rolls_back_on_close(tmp_path):
    p=tmp_path/'v'; create_vault(p)
    v=open_vault(p); v.connection.execute('BEGIN IMMEDIATE')
    v.connection.execute("insert into entries(parent_id,name,kind,content,created_at,updated_at) select id,'x','file','x',created_at,updated_at from entries where parent_id is null")
    v.close()
    with open_vault(p) as reopened:
        assert reopened.connection.execute('pragma integrity_check').fetchone()[0]=='ok'
        assert reopened.connection.execute("select count(*) from entries where name='x'").fetchone()[0]==0
