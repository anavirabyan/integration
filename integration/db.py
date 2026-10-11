"""PostgreSQL access (psycopg 3 + connection pool).

psycopg is imported lazily so that pure-logic modules and unit tests
can be imported without a database driver installed.
"""
from __future__ import annotations

import contextlib
import hashlib
import logging
from typing import Any, Iterator

from .config import settings

log = logging.getLogger(__name__)
_pool = None


def pool():
    """Create the pool on first use (one per process)."""
    global _pool
    if _pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        _pool = ConnectionPool(
            settings.database_url,
            min_size=1,
            max_size=10,
            kwargs={"row_factory": dict_row, "autocommit": False},
            open=True,
        )
    return _pool


@contextlib.contextmanager
def tx() -> Iterator[Any]:
    """One transaction: commit on success, rollback on any exception."""
    with pool().connection() as conn:
        with conn.transaction():
            yield conn


def fetch_all(sql: str, params: Any = None) -> list[dict]:
    with tx() as c:
        return c.execute(sql, params).fetchall()


def fetch_one(sql: str, params: Any = None) -> dict | None:
    with tx() as c:
        return c.execute(sql, params).fetchone()


def execute(sql: str, params: Any = None) -> int:
    with tx() as c:
        return c.execute(sql, params).rowcount


def json_param(value: Any):
    """Wrap a Python object for a jsonb parameter."""
    from psycopg.types.json import Jsonb

    return Jsonb(value)


def lock_key(name: str) -> int:
    """Stable 63-bit advisory-lock key for a job name."""
    return int.from_bytes(hashlib.sha1(name.encode()).digest()[:8], "big") & 0x7FFF_FFFF_FFFF_FFFF


@contextlib.contextmanager
def advisory_lock(name: str) -> Iterator[bool]:
    """Session advisory lock. Yields False if another instance holds it.

    Protects scheduled jobs from running twice, e.g. while Render runs the old
    and the new instance side by side during a deploy.
    """
    key = lock_key(name)
    with pool().connection() as conn:
        conn.autocommit = True
        got = conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (key,)).fetchone()["ok"]
        try:
            yield got
        finally:
            if got:
                conn.execute("SELECT pg_advisory_unlock(%s)", (key,))
            conn.autocommit = False


def ping() -> bool:
    try:
        fetch_one("SELECT 1 AS ok")
        return True
    except Exception:  # noqa: BLE001 — health check must never raise
        log.exception("database ping failed")
        return False
