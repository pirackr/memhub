"""Vault lifecycle: exclusive creation, validated opening, and transactions.

A vault is a single SQLite database file on the host. :func:`create_vault`
builds an empty vault with exactly one root; :func:`open_vault` reopens a
validated vault and returns a :class:`Vault` that manages a single SQLite
connection and short-lived transactions.

Design contract (see spec section 3-4):

* ``create_vault`` never overwrites and never writes outside the one host file
  it creates; a failed initialisation removes that file.
* ``open_vault`` never uses create-on-open mode, validates the application id
  (``0x4D454D48``) and schema version (1), and confirms the required schema
  objects exist.
* Every connection enables foreign keys, a five-second busy timeout,
  rollback-journal mode, and ``synchronous=FULL``.
* Failures map to stable typed errors with fixed exit statuses.
"""

from __future__ import annotations

import importlib.resources as _resources
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Union

from .errors import Busy, Conflict, InvalidInput, MemhubError, Missing, VaultFailure

__all__ = [
    "create_vault",
    "open_vault",
    "Vault",
]

# ASCII "MEMH" — the SQLite application id that marks a vault as a memhub one.
MEMHUB_APP_ID = 0x4D454D48

# First four bytes of every SQLite database file header.
_SQLITE_MAGIC = b"SQLite format 3\x00"

# V1 schema/user_version.
VAULT_SCHEMA_VERSION = 1

# Busy timeout in milliseconds; also the documented lock-wait budget.
BUSY_TIMEOUT_MS = 5000

PathLike = Union[str, Path]


def _coerce_path(value: PathLike) -> Path:
    return value if isinstance(value, Path) else Path(value)


def _load_schema_text() -> str:
    """Read ``schema.sql`` from the installed package resource."""
    text = _resources.files("memhub").joinpath("schema.sql").read_text(encoding="utf-8")
    return text


def _utcnow_iso() -> str:
    # Avoid a top-level datetime import cost; this is only for the root stamp.
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _safe_close(conn: sqlite3.Connection) -> None:
    try:
        conn.close()
    except sqlite3.Error:
        pass


def _safe_rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _safe_unlink(host: Path) -> None:
    try:
        host.unlink()
    except OSError:
        pass


def _is_lock_error(exc: sqlite3.Error) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def _translate_connection_error(exc: sqlite3.Error) -> MemhubError:
    if isinstance(exc, sqlite3.OperationalError):
        if _is_lock_error(exc):
            return Busy("database_busy", "database is busy: could not acquire a lock")
        return VaultFailure("database_error", f"sqlite operational error: {exc}")
    if isinstance(exc, sqlite3.IntegrityError):
        return Conflict("integrity_violation", f"schema constraint violated: {exc}")
    if isinstance(exc, sqlite3.DatabaseError):
        return VaultFailure("database_error", f"sqlite database error: {exc}")
    return VaultFailure("database_error", f"sqlite error: {exc}")


def _apply_connection_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.execute("PRAGMA synchronous = FULL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")


def _is_sqlite_file(host: Path) -> bool:
    try:
        with open(host, "rb") as handle:
            return handle.read(len(_SQLITE_MAGIC)) == _SQLITE_MAGIC
    except OSError:
        return False


def _assert_memhub_vault(conn: sqlite3.Connection) -> None:
    app_id = conn.execute("PRAGMA application_id").fetchone()[0]
    if app_id != MEMHUB_APP_ID:
        raise VaultFailure(
            "not_a_memhub_vault",
            f"application id {app_id:#010x} does not identify a memhub vault",
        )
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != VAULT_SCHEMA_VERSION:
        raise VaultFailure(
            "unsupported_vault_version",
            f"unsupported vault schema version {version} (expected {VAULT_SCHEMA_VERSION})",
        )
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if "entries" not in tables:
        raise VaultFailure("schema_validation_failed", "required table 'entries' is missing")
    root_count = conn.execute(
        "SELECT COUNT(*) FROM entries WHERE parent_id IS NULL"
    ).fetchone()[0]
    if root_count == 0:
        raise VaultFailure("schema_validation_failed", "vault has no root entry")


def create_vault(path: PathLike) -> None:
    """Exclusively create a new vault at ``path`` and return nothing.

    The host parent directory must already exist; an existing file is rejected.
    The database is initialised with UTF-8 default encoding, the memhub schema,
    application id ``0x4D454D48``, user version 1, and exactly one root. If any
    step fails, the single file this invocation created is removed.
    """
    host = _coerce_path(path)
    parent = host.parent
    if not parent.is_dir():
        raise InvalidInput(
            "host_parent_missing",
            f"host parent directory does not exist: {parent}",
        )
    if host.exists():
        raise Conflict(
            "vault_exists",
            f"a file already exists at {host}; create_vault never overwrites",
        )

    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(host))
        conn.row_factory = sqlite3.Row
        _apply_connection_pragmas(conn)
        # application_id must be set before any schema object exists.
        conn.execute(f"PRAGMA application_id = {MEMHUB_APP_ID}")
        conn.executescript(_load_schema_text())
        conn.execute(f"PRAGMA user_version = {VAULT_SCHEMA_VERSION}")
        stamp = _utcnow_iso()
        conn.execute(
            "INSERT INTO entries (parent_id, name, kind, content, created_at, updated_at) "
            "VALUES (NULL, '', 'directory', NULL, ?, ?)",
            (stamp, stamp),
        )
        conn.commit()
    except BaseException as exc:
        if conn is not None:
            _safe_close(conn)
        # Cleanup is intentionally restricted to the file this created.
        _safe_unlink(host)
        if isinstance(exc, MemhubError):
            raise
        if isinstance(exc, sqlite3.Error):
            raise _translate_connection_error(exc) from exc
        raise


def open_vault(path: PathLike) -> "Vault":
    """Open and validate an existing vault, returning a :class:`Vault`.

    Never uses create-on-open mode: a missing host raises :class:`Missing`
    without creating anything, and a non-SQLite file is rejected. The file's
    application id and schema version are validated and its required schema
    objects are checked before the connection is returned.
    """
    host = _coerce_path(path)
    if not host.exists():
        raise Missing("vault_missing", f"no vault exists at {host}")
    if not _is_sqlite_file(host):
        raise VaultFailure("not_a_memhub_vault", f"{host} is not a SQLite database")

    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(host))
        conn.row_factory = sqlite3.Row
        _apply_connection_pragmas(conn)
        _assert_memhub_vault(conn)
    except BaseException as exc:
        if conn is not None:
            _safe_close(conn)
        if isinstance(exc, MemhubError):
            raise
        if isinstance(exc, sqlite3.Error):
            raise _translate_connection_error(exc) from exc
        raise
    return Vault(host, conn)


class Vault:
    """A validated open vault backing a single SQLite connection.

    A :class:`Vault` is itself a context manager: entering returns the vault and
    leaving closes the connection. Use :meth:`transaction` to run read or
    immediate-write work; the connection is yielded, so callers issue SQL.
    """

    __slots__ = ("path", "connection")

    def __init__(self, path: Path, connection: sqlite3.Connection) -> None:
        self.path = path
        self.connection = connection

    def __enter__(self) -> "Vault":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    def close(self) -> None:
        connection = getattr(self, "connection", None)
        if connection is not None:
            _safe_close(connection)
            self.connection = None

    @contextmanager
    def transaction(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        """Yield the connection inside a transaction.

        With ``write=False`` a plain ``BEGIN`` gives a snapshot read; with
        ``write=True`` a ``BEGIN IMMEDIATE`` acquires the write reservation up
        front. On a raised exception the transaction is rolled back; otherwise
        it is committed. SQLite and host errors are translated to typed errors.
        """
        conn = self.connection
        started = False
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            else:
                conn.execute("BEGIN")
            started = True
            yield conn
        except BaseException as exc:  # noqa: BLE001 - re-translate and re-raise
            if started:
                _safe_rollback(conn)
            if isinstance(exc, MemhubError):
                raise
            if isinstance(exc, sqlite3.Error):
                raise _translate_connection_error(exc) from exc
            raise
        else:
            if started:
                try:
                    conn.commit()
                except sqlite3.OperationalError as locked:
                    if _is_lock_error(locked):
                        raise Busy(
                            "database_busy",
                            "database is busy: commit could not acquire a lock",
                        ) from locked
                    raise VaultFailure(
                        "database_error", f"database commit failed: {locked}"
                    ) from locked
                except sqlite3.Error as other:
                    raise VaultFailure(
                        "database_error", f"database commit failed: {other}"
                    ) from other
