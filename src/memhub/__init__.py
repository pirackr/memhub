"""memhub: a local SQLite text vault with filesystem-like virtual paths.

This package exposes the storage library used by the CLI and by library
callers. The public surface is the explicit export list below. No encoding
detector is imported here on package load; ``chardet`` is a pinned runtime
dependency that the import layer lazy-loads so ordinary startup stays cheap.
"""

from __future__ import annotations

from .errors import (
    Busy,
    Conflict,
    InvalidInput,
    MemhubError,
    Missing,
    Unsupported,
    VaultFailure,
)
from .documents import (
    Entry,
    WriteResult,
    write_file,
    write_in_transaction,
)
from .vault import Vault, create_vault, open_vault

__all__ = [
    "Vault",
    "create_vault",
    "open_vault",
    "Entry",
    "WriteResult",
    "write_file",
    "write_in_transaction",
    "MemhubError",
    "InvalidInput",
    "Missing",
    "Conflict",
    "Unsupported",
    "Busy",
    "VaultFailure",
]

__version__ = "1.0.0"
