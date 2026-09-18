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
    ReadResult,
    WriteResult,
    edit_file,
    read_file,
    write_file,
    write_in_transaction,
)
from .models import Edit, ListResult
from .tree import list_entries, remove_entry
from .vault import Vault, create_vault, open_vault

__all__ = [
    "Vault",
    "create_vault",
    "open_vault",
    "Entry",
    "ReadResult",
    "WriteResult",
    "ListResult",
    "Edit",
    "write_file",
    "write_in_transaction",
    "edit_file",
    "read_file",
    "list_entries",
    "remove_entry",
    "MemhubError",
    "InvalidInput",
    "Missing",
    "Conflict",
    "Unsupported",
    "Busy",
    "VaultFailure",
]

__version__ = "1.0.0"
