"""Typed failures for memhub.

Every error a memhub public surface can raise derives from :class:`MemhubError`.
Each carries a stable string ``code``, a human readable ``message``, and the
``exit_status`` the CLI must map the failure to. Keeping the mapping in one
place means the CLI never invents a status; it only forwards ``exit_status``.
"""

from __future__ import annotations

__all__ = [
    "MemhubError",
    "InvalidInput",
    "Missing",
    "Conflict",
    "Unsupported",
    "Busy",
    "VaultFailure",
]


class MemhubError(Exception):
    """Base class for every memhub failure.

    Subclasses fix the ``exit_status``; call sites fix the stable ``code`` and a
    human readable ``message``. ``code`` is part of the contract and must not
    change between versions.
    """

    #: Default status when a subclass forgets to set one.
    exit_status = 1

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{type(self).__name__}(code={self.code!r}, exit_status={self.exit_status}, message={self.message!r})"


class InvalidInput(MemhubError):
    """Invalid arguments, paths, JSON, or edit specification.

    Maps to CLI exit status 2.
    """

    exit_status = 2


class Missing(MemhubError):
    """Missing vault or entry.

    Maps to CLI exit status 3.
    """

    exit_status = 3


class Conflict(MemhubError):
    """Conflict: existing destination, stale hash, or ambiguous edit.

    Maps to CLI exit status 4.
    """

    exit_status = 4


class Unsupported(MemhubError):
    """Unsupported input/encoding/type.

    Maps to CLI exit status 5.
    """

    exit_status = 5


class Busy(MemhubError):
    """Database busy after the configured timeout.

    Maps to CLI exit status 6 and is safe to retry.
    """

    exit_status = 6


class VaultFailure(MemhubError):
    """Vault schema/integrity, SQLite I/O, or host I/O failure.

    Maps to CLI exit status 7.
    """

    exit_status = 7
