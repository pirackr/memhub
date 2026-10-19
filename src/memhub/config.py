"""Store configuration: which SQLite file is the active memhub store.

Resolution order for resolve_store():
1. MEMHUB_DB env var (a file path) wins.
2. Otherwise the default_store entry in ~/.memhub/config.toml.

Config format::

    default_store = "personal"

    [stores]
    personal = "/home/u/.memhub/personal.sqlite"
    work = "/srv/work.sqlite"
"""

import os
from pathlib import Path

import tomli_w

from .store import MemhubError


def config_path() -> Path:
    """Path to the memhub config file (~/.memhub/config.toml).

    MEMHUB_HOME overrides the ~/.memhub directory (test hook).
    """
    home = os.environ.get("MEMHUB_HOME") or "~/.memhub"
    return Path(os.path.expandvars(os.path.expanduser(home))) / "config.toml"


def _expand(raw: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(raw)))


def _load_config() -> dict:
    """Load and validate the config file; {} if it does not exist."""
    import tomllib

    path = config_path()
    if not path.exists():
        return {}
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise MemhubError(f"invalid config file {path}: {exc}") from exc
    stores = data.get("stores")
    if stores is not None and not isinstance(stores, dict):
        raise MemhubError(f"invalid config file {path}: 'stores' must be a table")
    default = data.get("default_store")
    if default is not None and not isinstance(default, str):
        raise MemhubError(f"invalid config file {path}: 'default_store' must be a string")
    return data


def resolve_store() -> Path:
    """Return the path of the active store database file."""
    env_db = os.environ.get("MEMHUB_DB")
    if env_db:
        return _expand(env_db)
    data = _load_config()
    name = data.get("default_store")
    if name is None:
        raise MemhubError("no store configured; run memhub init")
    stores = data.get("stores") or {}
    if name not in stores:
        raise MemhubError(f"default_store {name!r} is not a registered store")
    raw = stores[name]
    if not isinstance(raw, str):
        raise MemhubError(f"store {name!r} path must be a string")
    return _expand(raw)


def get_registered_path(name: str) -> Path | None:
    """Return the registered path of store `name`, or None if not registered."""
    stores = _load_config().get("stores") or {}
    raw = stores.get(name)
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise MemhubError(f"store {name!r} path must be a string")
    return _expand(raw)


def register_store(name: str, db_path: Path, set_default: bool) -> None:
    """Register a store in the config file, creating/updating it atomically.

    The first registered store becomes the default; set_default=True flips
    default_store to this one.
    """
    data = _load_config()
    stores = data.setdefault("stores", {})
    stores[name] = str(db_path)
    if set_default or "default_store" not in data:
        data["default_store"] = name
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    import tempfile

    import tomli_w

    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".toml")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(tomli_w.dumps(data).encode("utf-8"))
        os.replace(tmp_name, path)
    except BaseException:
        os.unlink(tmp_name)
        raise
