"""Shared pytest fixtures for procdiag tests."""

from __future__ import annotations

import pytest
from shellrun import Stubs

from procdiag.storage import Storage


@pytest.fixture
def tmp_db(tmp_path):
    """Provide a fresh Storage instance backed by a temporary file."""
    db_path = tmp_path / "test.db"
    with Storage(db_path) as store:
        yield store


@pytest.fixture
def stubs(tmp_path_factory) -> Stubs:
    """Stand-in commands on PATH for pasting a suggested command into a shell."""
    return Stubs(tmp_path_factory.mktemp("stubs"))
