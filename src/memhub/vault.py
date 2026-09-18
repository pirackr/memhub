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
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Iterator, Union

from .errors import Busy, Conflict, InvalidInput, MemhubError, Missing, VaultFailure

__all__ = [
    "create_vault",
    "open_vault",
    "audit_vault",
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


def _safe_unlink(host: Path, identity: tuple[int, int] | None = None) -> None:
    """Unlink only the staging inode created by this process.

    The identity guard prevents cleanup from deleting a path that a hostile or
    concurrent directory writer replaced after staging was created.
    """
    try:
        if identity is not None:
            current = host.lstat()
            if (current.st_dev, current.st_ino) != identity:
                return
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


@lru_cache(maxsize=1)
def _canonical_schema_objects() -> dict[tuple[str, str], str]:
    """Return SQLite's canonicalized SQL for every required schema object."""
    reference = sqlite3.connect(':memory:')
    try:
        reference.executescript(_load_schema_text())
        return {(row[0], row[1]): row[2] for row in reference.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' AND type IN ('table','index','trigger')"
        )}
    finally:
        reference.close()


def _assert_memhub_vault(conn: sqlite3.Connection, *, audit: bool = False) -> None:
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
    objects = {(row[0], row[1]): row[2] for row in conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' AND type IN ('table','index','trigger')"
    )}
    canonical = _canonical_schema_objects()
    if objects != canonical:
        raise VaultFailure(
            "schema_validation_failed",
            "schema object set differs from the exact canonical definitions",
        )
    columns = [(r[1], r[2], r[3], r[5]) for r in conn.execute("PRAGMA table_info(entries)")]
    expected = [('id', 'INTEGER', 0, 1), ('parent_id', 'INTEGER', 0, 0),
                ('name', 'TEXT', 1, 0), ('kind', 'TEXT', 1, 0),
                ('content', 'TEXT', 0, 0), ('created_at', 'TEXT', 1, 0),
                ('updated_at', 'TEXT', 1, 0)]
    if columns != expected:
        raise VaultFailure("schema_validation_failed", "entries columns do not match schema")
    root = conn.execute(
        "SELECT name, kind, content FROM entries WHERE parent_id IS NULL"
    ).fetchall()
    if len(root) != 1 or tuple(root[0]) != ('', 'directory', None):
        raise VaultFailure("schema_validation_failed", "vault root is missing or invalid")
    if not audit:
        return
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    foreign = conn.execute("PRAGMA foreign_key_check").fetchone()
    invalid = conn.execute("""SELECT COUNT(*) FROM entries WHERE
        kind NOT IN ('file','directory') OR
        (kind='file' AND content IS NULL) OR (kind='directory' AND content IS NOT NULL) OR
        (parent_id IS NULL AND NOT(name='' AND kind='directory' AND content IS NULL)) OR
        (parent_id IS NOT NULL AND (name='' OR instr(name,'/')>0)) OR
        (parent_id IS NOT NULL AND NOT EXISTS
          (SELECT 1 FROM entries p WHERE p.id=entries.parent_id AND p.kind='directory'))""").fetchone()[0]
    # UNION (not UNION ALL) makes traversal terminate after at most one row per
    # entry even if corruption has introduced a cycle. It therefore has no
    # dependency on Python or SQLite recursion-depth limits for deep trees.
    unreachable = conn.execute("""WITH RECURSIVE reachable(id) AS (
        SELECT id FROM entries WHERE parent_id IS NULL
        UNION
        SELECT child.id FROM entries AS child
        JOIN reachable AS parent ON child.parent_id = parent.id
    )
    SELECT (SELECT COUNT(*) FROM entries) - (SELECT COUNT(*) FROM reachable)""").fetchone()[0]
    if integrity != 'ok' or foreign is not None or invalid or unreachable:
        raise VaultFailure("schema_validation_failed", "vault integrity or tree invariants failed")


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
    conn: sqlite3.Connection | None = None
    temp: Path | None = None
    staging_identity: tuple[int, int] | None = None
    fd: int | None = None
    try:
        # Keep mkstemp's mode-0600 pathname and descriptor alive throughout
        # construction.  Never create SQLite at a name that was briefly freed.
        fd, temp_name = tempfile.mkstemp(prefix=f'.{host.name}.', suffix='.tmp', dir=parent)
        os.fchmod(fd, 0o600)
        stat = os.fstat(fd)
        staging_identity = (stat.st_dev, stat.st_ino)
        temp = Path(temp_name)
        conn = sqlite3.connect(str(temp))
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
        conn.close()
        conn = None
        current = temp.lstat()
        if (current.st_dev, current.st_ino) != staging_identity:
            raise VaultFailure('staging_replaced', 'secure vault staging file was replaced')
        try:
            os.link(temp, host)
        except FileExistsError as conflict:
            raise Conflict("vault_exists", f"a file already exists at {host}; create_vault never overwrites") from conflict
        _safe_unlink(temp, staging_identity)
        temp = None
        os.close(fd)
        fd = None
        directory = os.open(parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException as exc:
        if conn is not None:
            _safe_close(conn)
        if temp is not None:
            _safe_unlink(temp, staging_identity)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if isinstance(exc, MemhubError):
            raise
        if isinstance(exc, sqlite3.Error):
            raise _translate_connection_error(exc) from exc
        raise


def open_vault(path: PathLike, *, audit: bool = False) -> "Vault":
    """Open and validate an existing vault, returning a :class:`Vault`.

    Normal opening performs fast identity, version, canonical-schema, column,
    and root checks. ``audit=True`` additionally scans database integrity,
    foreign keys, and all tree invariants; use it for untrusted or suspected
    corrupt data.

    Never uses create-on-open mode: a missing host raises :class:`Missing`
    without creating anything, and a non-SQLite file is rejected. The file's
    application id and schema version are validated and its required schema
    objects are checked before the connection is returned.
    """
    host = _coerce_path(path)
    conn: sqlite3.Connection | None = None
    try:
        # URI mode=rw is the non-creating guarantee; all validation is against
        # this connected inode rather than a racy preflight pathname read.
        uri = host.absolute().as_uri() + '?mode=rw'
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        _apply_connection_pragmas(conn)
        _assert_memhub_vault(conn, audit=audit)
    except BaseException as exc:
        if conn is not None:
            _safe_close(conn)
        if isinstance(exc, MemhubError):
            raise
        if isinstance(exc, sqlite3.OperationalError) and 'unable to open database file' in str(exc).lower():
            raise Missing("vault_missing", f"no vault exists or is accessible at {host}") from exc
        if isinstance(exc, sqlite3.Error):
            raise _translate_connection_error(exc) from exc
        raise
    return Vault(host, conn)


def audit_vault(path: PathLike) -> None:
    """Exhaustively validate an existing vault and close it."""
    with open_vault(path, audit=True):
        return None


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
                    _safe_rollback(conn)
                    if _is_lock_error(locked):
                        raise Busy(
                            "database_busy",
                            "database is busy: commit could not acquire a lock",
                        ) from locked
                    raise VaultFailure(
                        "database_error", f"database commit failed: {locked}"
                    ) from locked
                except sqlite3.Error as other:
                    _safe_rollback(conn)
                    raise VaultFailure(
                        "database_error", f"database commit failed: {other}"
                    ) from other
