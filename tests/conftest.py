"""Shared pytest fixtures for procdiag tests."""

from __future__ import annotations

import pytest
from shellrun import Stubs

from procdiag import recorder as recorder_mod
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


@pytest.fixture(autouse=True)
def _a_tick_waits_for_its_read(monkeypatch):
    """The recorder waits for the read of each tick, as it did on one thread.

    The service and kernel-log reads run on their own thread, and the job that
    starts one waits only a moment for it.  A fake clock runs the ticks without
    real time passing, so a read that needs a few real milliseconds would still
    be running at the next tick and that tick would be skipped.
    ``test_recorder_slow_reads`` sets the real wait.
    """
    monkeypatch.setattr(recorder_mod, "_EVENT_READ_WAIT_SEC", 30.0)
