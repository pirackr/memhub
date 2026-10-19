"""End-to-end tests for the memhub CLI.

These call cli.main([...]) directly with MEMHUB_HOME/MEMHUB_DB pointed at a
temp dir, capturing stdout/stderr/stdin as raw bytes so byte-exactness of
`cat` is verifiable.
"""

import io
import json
import os
import sys
import tomllib
from pathlib import Path

import pytest

from memhub import cli, config


@pytest.fixture(autouse=True)
def memhub_env(tmp_path, monkeypatch):
    """Point MEMHUB_HOME at a temp dir and clear MEMHUB_DB."""
    home = tmp_path / "home"
    monkeypatch.setenv("MEMHUB_HOME", str(home))
    monkeypatch.delenv("MEMHUB_DB", raising=False)
    return tmp_path


class _Capture:
    """A file-like sink with a .buffer, capturing raw bytes."""

    def __init__(self):
        self._buf = io.BytesIO()
        self.buffer = self._buf

    def write(self, s):
        self._buf.write(s.encode("utf-8") if isinstance(s, str) else s)

    def flush(self):
        pass

    def getvalue(self):
        return self._buf.getvalue()


class _Stdin:
    def __init__(self, data: bytes):
        self.buffer = io.BytesIO(data)


def run(argv, stdin=b""):
    """Run cli.main(argv), returning (code, stdout_bytes, stderr_bytes)."""
    out, err = _Capture(), _Capture()
    saved = sys.stdout, sys.stderr, sys.stdin
    sys.stdout, sys.stderr, sys.stdin = out, err, _Stdin(stdin)
    try:
        code = cli.main(argv)
    finally:
        sys.stdout, sys.stderr, sys.stdin = saved
    return code, out.getvalue(), err.getvalue()


def jout(out: bytes):
    return json.loads(out.decode("utf-8"))


# --- init -----------------------------------------------------------------


def test_init_prints_json(memhub_env):
    code, out, err = run(["init", "personal"])
    assert code == 0
    data = jout(out)
    assert data["store"] == "personal"
    assert data["path"].endswith("personal.sqlite")


def test_init_default_name(memhub_env):
    code, out, err = run(["init"])
    assert code == 0
    assert jout(out)["store"] == "default"


def test_init_with_path(memhub_env, tmp_path):
    db = tmp_path / "custom" / "my.sqlite"
    code, out, err = run(["init", "custom", "--path", str(db)])
    assert code == 0
    assert jout(out)["path"] == str(db)
    assert db.exists()


def test_init_twice_first_stays_default(memhub_env):
    run(["init", "personal"])
    run(["init", "work"])
    cfg = tomllib.loads(config.config_path().read_text())
    assert cfg["default_store"] == "personal"
    assert set(cfg["stores"]) == {"personal", "work"}


# --- cp -------------------------------------------------------------------


def test_cp_stdin_and_cat(memhub_env):
    run(["init"])
    code, out, err = run(["cp", "-", "/greeting.txt"], stdin=b"hello\n")
    assert code == 0
    mem = jout(out)
    assert mem["path"] == "/greeting.txt"
    assert mem["content_type"] == "text/plain"
    assert mem["size"] == 6
    code, out, err = run(["cat", "/greeting.txt"])
    assert code == 0
    assert out == b"hello\n"


def test_cp_local_file(memhub_env, tmp_path):
    run(["init"])
    f = tmp_path / "note.txt"
    f.write_bytes(b"local content")
    code, out, err = run(["cp", str(f), "/docs/note.txt"])
    assert code == 0
    assert jout(out)["path"] == "/docs/note.txt"
    code, out, err = run(["cat", "/docs/note.txt"])
    assert out == b"local content"


def test_cp_trailing_slash_dest(memhub_env, tmp_path):
    run(["init"])
    f = tmp_path / "note.txt"
    f.write_bytes(b"x")
    code, out, err = run(["cp", str(f), "/docs/"])
    assert code == 0
    assert jout(out)["path"] == "/docs/note.txt"


def test_cp_stdin_dir_dest_rejected(memhub_env):
    run(["init"])
    code, out, err = run(["cp", "-", "/docs/"], stdin=b"x")
    assert code == 1
    assert b"Traceback" not in err
    assert err.strip() != b""


def test_cp_store_to_store(memhub_env):
    run(["init"])
    run(["cp", "-", "/mem.txt"], stdin=b"content")
    code, out, err = run(["cp", "/mem.txt", "/copy.txt"])
    assert code == 0
    assert jout(out)["path"] == "/copy.txt"
    code, out, err = run(["cat", "/copy.txt"])
    assert out == b"content"


def test_cp_local_directory_rejected(memhub_env, tmp_path):
    run(["init"])
    d = tmp_path / "adir"
    d.mkdir()
    code, out, err = run(["cp", str(d), "/x"])
    assert code == 1
    assert b"Traceback" not in err


def test_cp_content_type_flag(memhub_env):
    run(["init"])
    code, out, err = run(
        ["cp", "-", "/data.bin", "--content-type", "application/octet-stream"],
        stdin=b"\x00\xff",
    )
    assert code == 0
    assert jout(out)["content_type"] == "application/octet-stream"


def test_cp_binary_roundtrip(memhub_env):
    run(["init"])
    data = b"\x00\xff\xfe binary bytes"
    code, out, err = run(["cp", "-", "/bin.dat"], stdin=data)
    assert code == 0
    assert jout(out)["content_type"] == "application/octet-stream"
    code, out, err = run(["cat", "/bin.dat"])
    assert out == data


def test_cp_overwrites(memhub_env):
    run(["init"])
    run(["cp", "-", "/a.txt"], stdin=b"first")
    code, out, err = run(["cp", "-", "/a.txt"], stdin=b"second!")
    assert code == 0
    assert jout(out)["size"] == 7
    code, out, err = run(["cat", "/a.txt"])
    assert out == b"second!"


# --- cat ------------------------------------------------------------------


def test_cat_missing_clean_error(memhub_env):
    run(["init"])
    code, out, err = run(["cat", "/nope.txt"])
    assert code == 1
    assert b"Traceback" not in err
    assert err.strip() != b""


def test_cat_directory_is_error(memhub_env):
    run(["init"])
    run(["cp", "-", "/dir/a.txt"], stdin=b"a")
    code, out, err = run(["cat", "/dir"])
    assert code == 1
    assert b"Traceback" not in err
    assert b"ls" in err


# --- ls -------------------------------------------------------------------


def test_ls_default_root(memhub_env):
    run(["init"])
    run(["cp", "-", "/a.txt"], stdin=b"a")
    run(["cp", "-", "/dir/b.txt"], stdin=b"b")
    code, out, err = run(["ls"])
    assert code == 0
    entries = jout(out)
    paths = {e["path"] for e in entries}
    assert paths == {"/a.txt", "/dir"}
    d = next(e for e in entries if e["path"] == "/dir")
    assert d["is_dir"] is True
    assert d["content_type"] is None
    assert d["size"] is None
    assert d["created_at"] is None
    assert d["updated_at"] is None


def test_ls_recursive(memhub_env):
    run(["init"])
    run(["cp", "-", "/dir/b.txt"], stdin=b"b")
    run(["cp", "-", "/dir/sub/c.txt"], stdin=b"c")
    code, out, err = run(["ls", "-R"])
    assert code == 0
    paths = {e["path"] for e in jout(out)}
    assert paths == {"/dir/b.txt", "/dir/sub/c.txt"}


def test_ls_empty_store(memhub_env):
    run(["init"])
    code, out, err = run(["ls"])
    assert code == 0
    assert jout(out) == []


# --- stat -----------------------------------------------------------------


def test_stat(memhub_env):
    run(["init"])
    run(["cp", "-", "/a.txt"], stdin=b"hello")
    code, out, err = run(["stat", "/a.txt"])
    assert code == 0
    m = jout(out)
    assert m["path"] == "/a.txt"
    assert m["size"] == 5
    assert set(m) == {"id", "path", "content_type", "size", "created_at", "updated_at"}


# --- mv -------------------------------------------------------------------


def test_mv(memhub_env):
    run(["init"])
    run(["cp", "-", "/a.txt"], stdin=b"x")
    code, out, err = run(["mv", "/a.txt", "/b.txt"])
    assert code == 0
    assert jout(out)["path"] == "/b.txt"
    code, out, err = run(["cat", "/b.txt"])
    assert out == b"x"


# --- rm -------------------------------------------------------------------


def test_rm(memhub_env):
    run(["init"])
    run(["cp", "-", "/a.txt"], stdin=b"x")
    code, out, err = run(["rm", "/a.txt"])
    assert code == 0
    assert jout(out) == {"removed": 1}


def test_rm_recursive(memhub_env):
    run(["init"])
    run(["cp", "-", "/dir/b.txt"], stdin=b"b")
    run(["cp", "-", "/dir/c.txt"], stdin=b"c")
    code, out, err = run(["rm", "/dir", "-r"])
    assert code == 0
    assert jout(out) == {"removed": 2}


# --- mkdir ----------------------------------------------------------------


def test_mkdir_noop(memhub_env):
    run(["init"])
    code, out, err = run(["mkdir", "/some/dir"])
    assert code == 0


def test_mkdir_invalid_path(memhub_env):
    run(["init"])
    code, out, err = run(["mkdir", "/bad//path"])
    assert code == 1
    assert b"Traceback" not in err


# --- store resolution / argparse -----------------------------------------


def test_no_store_configured(memhub_env):
    code, out, err = run(["cat", "/x"])
    assert code == 1
    assert b"Traceback" not in err
    assert b"no store configured" in err


def test_unknown_command_exit_2(memhub_env):
    code, out, err = run(["bogus"])
    assert code == 2


def test_no_command_exit_2(memhub_env):
    code, out, err = run([])
    assert code == 2
