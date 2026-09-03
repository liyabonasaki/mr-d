"""
Shared test fixtures and helpers.

Mock strategy
-------------
All tests are pure unit tests — no real database is required.
`get_conn()` is patched with a factory that returns a MagicMock connection
whose cursor() acts as a context manager and returns a MagicMock cursor.
Each test configures cursor.fetchone / fetchall / execute return values
directly on the mock.

Threading state
---------------
The worker uses module-level threading.Events (_paused, _stop).
The worker_events fixture resets them before and after each test so
state never leaks between tests.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

import app.worker as worker_module


# ---------------------------------------------------------------------------
# Connection / cursor mock factory
# ---------------------------------------------------------------------------

def make_cursor(fetchone=None, fetchall=None):
    """Return a MagicMock cursor pre-configured with fetchone/fetchall."""
    cur = MagicMock()
    cur.fetchone.return_value = fetchone
    cur.fetchall.return_value = fetchall if fetchall is not None else []
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    return cur


def make_conn(cursor=None):
    """Return a MagicMock connection whose cursor() yields the given cursor."""
    conn = MagicMock()
    _cursor = cursor or make_cursor()
    conn.cursor.return_value = _cursor
    return conn


@contextmanager
def fake_get_conn_factory(conn):
    """Wrap a mock connection as a get_conn() context manager."""
    yield conn


def patch_get_conn(module_path: str, conn):
    """Return a patch context manager that replaces get_conn in module_path."""
    return patch(module_path, lambda: fake_get_conn_factory(conn))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def worker_events():
    """Reset worker threading events before and after every test."""
    worker_module._paused.clear()
    worker_module._stop.clear()
    yield
    worker_module._paused.clear()
    worker_module._stop.clear()
