"""PostgreSQL connection-pool and SQL migration foundation.

The pool is deliberately not opened at import time. Gunicorn must open it from
a post-fork worker hook so no live libpq connections are inherited by workers.
The existing SQLite application remains authoritative until the later cutover.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_POOL: ConnectionPool | None = None
_POOL_PID: int | None = None


def _dsn() -> str:
    value = os.environ.get("CARD_SCANNER_POSTGRES_DSN", "").strip()
    if not value:
        raise RuntimeError("CARD_SCANNER_POSTGRES_DSN is not configured")
    return value


def open_pool() -> ConnectionPool:
    """Open a bounded pool in the current process (call after worker fork)."""
    global _POOL, _POOL_PID
    pid = os.getpid()
    if _POOL is not None and _POOL_PID == pid:
        return _POOL

    # Never reuse a pool created in a different process. In normal operation
    # the pool is unopened before fork and this branch is only defensive.
    _POOL = ConnectionPool(
        conninfo=_dsn(),
        min_size=max(1, int(os.environ.get("CARD_SCANNER_PG_POOL_MIN", "1"))),
        max_size=max(1, int(os.environ.get("CARD_SCANNER_PG_POOL_MAX", "5"))),
        kwargs={"row_factory": dict_row},
        open=False,
        name=f"cardscanner-{pid}",
    )
    _POOL.open(wait=True)
    _POOL_PID = pid
    return _POOL


def get_pool() -> ConnectionPool:
    """Return the current process's already-open pool."""
    if _POOL is None or _POOL_PID != os.getpid():
        raise RuntimeError("PostgreSQL pool is not open in this process")
    return _POOL


def close_pool() -> None:
    """Close this process's pool during worker shutdown."""
    global _POOL, _POOL_PID
    if _POOL is not None and _POOL_PID == os.getpid():
        _POOL.close()
    _POOL = None
    _POOL_PID = None


def connection():
    """Borrow a connection; normal context exit commits, exceptions roll back."""
    return get_pool().connection()


def run_migrations(migrations_dir: str | Path | None = None) -> list[str]:
    """Apply ordered *.sql migrations exactly once, transactionally."""
    directory = Path(migrations_dir) if migrations_dir else Path(__file__).parent / "db" / "migrations"
    applied_now: list[str] = []
    with connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS public.cardscanner_schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        # Serialize migration runners across web workers and updater processes.
        conn.execute("SELECT pg_advisory_xact_lock(731942810552)")
        applied = {
            row["version"]
            for row in conn.execute(
                "SELECT version FROM public.cardscanner_schema_migrations"
            ).fetchall()
        }
        for path in sorted(directory.glob("[0-9][0-9][0-9][0-9]_*.sql")):
            if path.name in applied:
                continue
            sql = path.read_text(encoding="utf-8")
            if not sql.strip():
                raise RuntimeError(f"Refusing to apply empty migration: {path.name}")
            conn.execute(sql, prepare=False)
            conn.execute(
                "INSERT INTO public.cardscanner_schema_migrations(version) VALUES (%s)",
                (path.name,),
            )
            applied_now.append(path.name)
    return applied_now


def health_check() -> dict[str, str]:
    """Run a lightweight database readiness query."""
    with connection() as conn:
        row = conn.execute("SELECT current_database() AS database, version() AS version").fetchone()
    return {"database": row["database"], "version": row["version"]}
