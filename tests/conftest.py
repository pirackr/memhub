"""Shared pytest fixtures for the memhub test suite.

Every test runs against disposable fixtures only. Later tasks extend this module
with vault/source helpers; keep additions backward compatible.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

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
