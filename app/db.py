"""
Database connection pool and helpers.

Uses psycopg v3 (the `psycopg` package) which ships pre-built binary
wheels for Python 3.12–3.14, avoiding the pg_config / source-build
requirement that psycopg2-binary has on newer Python versions.

psycopg v3 differences from psycopg2 that matter here:
  - Import is `psycopg`, not `psycopg2`.
  - Pool lives in the separate `psycopg_pool` package.
  - `row_factory=dict_row` replaces RealDictCursor.
  - Errors are in `psycopg.errors`, e.g. psycopg.errors.UniqueViolation.
  - Python lists are adapted to PG arrays automatically (ANY(%s) works).
  - pool.connection() is itself a context manager; we wrap it so callers
    keep the same `with get_conn() as conn` pattern as before.

Usage
-----
    from app.db import get_conn

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT ...")
            rows = cur.fetchall()
        conn.commit()
"""

from contextlib import contextmanager
from typing import Generator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import settings

# ---------------------------------------------------------------------------
# Pool – created once at import time; re-used for the process lifetime.
# ---------------------------------------------------------------------------
_pool: ConnectionPool | None = None


def _get_pool() -> ConnectionPool:
    global _pool
    if _pool is None or _pool.closed:
        _pool = ConnectionPool(
            conninfo=settings.database_url,
            min_size=1,
            max_size=10,
            kwargs={"row_factory": dict_row, "autocommit": False},
            open=True,
        )
    return _pool


@contextmanager
def get_conn() -> Generator[psycopg.Connection, None, None]:
    """Yield a connection from the pool; return it on exit.

    The caller must call conn.commit() explicitly.
    On any unhandled exception the connection is rolled back before
    being returned to the pool so it is left in a clean state.
    """
    p = _get_pool()
    # pool.connection() returns the connection AND handles pool return on exit.
    # We wrap it ourselves so we can guarantee rollback on exception.
    conn = p.getconn()
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        p.putconn(conn)


def close_pool() -> None:
    """Gracefully close all pooled connections (called on shutdown)."""
    global _pool
    if _pool and not _pool.closed:
        _pool.close()
    _pool = None
