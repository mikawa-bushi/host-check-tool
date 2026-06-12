"""Shared pytest fixtures for procdiag tests."""

from __future__ import annotations

import pytest

from procdiag.storage import Storage


@pytest.fixture
def tmp_db(tmp_path):
    """Provide a fresh Storage instance backed by a temporary file."""
    db_path = tmp_path / "test.db"
    with Storage(db_path) as store:
        yield store
