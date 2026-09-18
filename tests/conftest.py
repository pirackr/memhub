"""Shared pytest fixtures for the memhub test suite.

Every test runs against disposable fixtures only. Later tasks extend this module
with vault/source helpers; keep additions backward compatible.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import memhub

# Support running the suite directly from the checkout (src layout) without a
# prior editable install; an installed test environment makes this redundant
# but harmless.
_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.fixture
def host_dir(tmp_path) -> Path:
    """A fresh, disposable host directory that already exists for a vault."""
    return tmp_path


@pytest.fixture
def vault(host_dir):
    """A freshly created, open memhub vault on a disposable host file.

    The caller owns the returned :class:`~memhub.Vault` and may reach
    ``.connection`` for direct SQL. Leaving the fixture closes the connection
    but never commits, so a test's uncommitted writes are discarded along with
    the file.
    """
    host = host_dir / "vault.db"
    memhub.create_vault(host)
    opened = memhub.open_vault(host)
    try:
        yield opened
    finally:
        opened.close()
