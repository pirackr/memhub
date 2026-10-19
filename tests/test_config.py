"""Tests for memhub.config store resolution."""

import pathlib
import tomllib

import pytest

from memhub import config
from memhub.store import MemhubError


@pytest.fixture(autouse=True)
def memhub_home(tmp_path, monkeypatch):
    """Point MEMHUB_HOME at a temp dir and clear MEMHUB_DB."""
    home = tmp_path / "memhub_home"
    monkeypatch.setenv("MEMHUB_HOME", str(home))
    monkeypatch.delenv("MEMHUB_DB", raising=False)
    return home


def write_config(text: str):
    home = config.config_path().parent
    home.mkdir(parents=True, exist_ok=True)
    config.config_path().write_text(text)


# --- config_path ---------------------------------------------------------


def test_config_path_under_memhub_home(memhub_home):
    assert config.config_path() == memhub_home / "config.toml"


def test_config_path_default(monkeypatch):
    monkeypatch.delenv("MEMHUB_HOME", raising=False)
    assert config.config_path().name == "config.toml"
    assert str(config.config_path()).endswith("/.memhub/config.toml")


# --- resolve_store: failures ----------------------------------------------


def test_resolve_no_config_no_env_raises():
    with pytest.raises(MemhubError, match="no store configured; run memhub init"):
        config.resolve_store()


def test_resolve_empty_config_raises():
    write_config("")
    with pytest.raises(MemhubError, match="no store configured; run memhub init"):
        config.resolve_store()


def test_resolve_invalid_toml_raises():
    write_config("this is not [ valid toml")
    with pytest.raises(MemhubError, match="config"):
        config.resolve_store()


def test_resolve_stores_not_a_table_raises():
    write_config('default_store = "personal"\nstores = "oops"\n')
    with pytest.raises(MemhubError, match="stores"):
        config.resolve_store()


def test_resolve_default_store_unknown_raises():
    write_config('default_store = "nope"\n\n[stores]\npersonal = "/x/p.sqlite"\n')
    with pytest.raises(MemhubError, match="nope"):
        config.resolve_store()


# --- resolve_store: MEMHUB_DB ----------------------------------------------


def test_resolve_env_wins_over_config(tmp_path, monkeypatch):
    write_config('default_store = "personal"\n\n[stores]\npersonal = "/x/p.sqlite"\n')
    env_db = tmp_path / "from_env.sqlite"
    monkeypatch.setenv("MEMHUB_DB", str(env_db))
    assert config.resolve_store() == env_db


def test_resolve_env_expands_tilde(monkeypatch):
    monkeypatch.setenv("HOME", "/home/testuser")
    monkeypatch.setenv("MEMHUB_DB", "~/memhub.sqlite")
    assert config.resolve_store() == pathlib.Path("/home/testuser/memhub.sqlite")


def test_resolve_env_expands_vars(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMHUB_TEST_DIR", str(tmp_path))
    monkeypatch.setenv("MEMHUB_DB", "$MEMHUB_TEST_DIR/work.sqlite")
    assert config.resolve_store() == tmp_path / "work.sqlite"


# --- resolve_store: default store ------------------------------------------


def test_resolve_default_store(tmp_path):
    write_config(
        'default_store = "work"\n\n[stores]\n'
        f'personal = "{tmp_path}/p.sqlite"\nwork = "{tmp_path}/w.sqlite"\n'
    )
    assert config.resolve_store() == tmp_path / "w.sqlite"


def test_resolve_registered_path_expands(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMHUB_TEST_DIR", str(tmp_path))
    write_config('default_store = "work"\n\n[stores]\nwork = "$MEMHUB_TEST_DIR/w.sqlite"\n')
    assert config.resolve_store() == tmp_path / "w.sqlite"


# --- register_store ----------------------------------------------------------


def test_register_store_creates_file(tmp_path):
    config.register_store("personal", tmp_path / "personal.sqlite", set_default=True)
    data = tomllib.loads(config.config_path().read_text())
    assert data["default_store"] == "personal"
    assert data["stores"] == {"personal": str(tmp_path / "personal.sqlite")}


def test_register_store_second_call_adds_store(tmp_path):
    config.register_store("personal", tmp_path / "p.sqlite", set_default=True)
    config.register_store("work", tmp_path / "w.sqlite", set_default=False)
    data = tomllib.loads(config.config_path().read_text())
    assert data["default_store"] == "personal"
    assert data["stores"] == {
        "personal": str(tmp_path / "p.sqlite"),
        "work": str(tmp_path / "w.sqlite"),
    }


def test_register_store_set_default_flips(tmp_path):
    config.register_store("personal", tmp_path / "p.sqlite", set_default=True)
    config.register_store("work", tmp_path / "w.sqlite", set_default=True)
    data = tomllib.loads(config.config_path().read_text())
    assert data["default_store"] == "work"


def test_register_store_updates_existing_entry(tmp_path):
    config.register_store("work", tmp_path / "old.sqlite", set_default=True)
    config.register_store("work", tmp_path / "new.sqlite", set_default=False)
    data = tomllib.loads(config.config_path().read_text())
    assert data["stores"] == {"work": str(tmp_path / "new.sqlite")}


def test_register_store_resolves_round_trip(tmp_path):
    config.register_store("work", tmp_path / "w.sqlite", set_default=True)
    assert config.resolve_store() == tmp_path / "w.sqlite"


def test_register_store_creates_parent_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMHUB_HOME", str(tmp_path / "deep" / "nested"))
    config.register_store("personal", tmp_path / "p.sqlite", set_default=True)
    assert config.config_path().exists()
