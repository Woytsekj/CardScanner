"""Post-fork PostgreSQL initialization for the staged application migration."""
from __future__ import annotations

import os

from postgres_db import close_pool, connection, open_pool, run_migrations


def expected_database_name() -> str:
    """Return the explicitly expected database, defaulting to development."""
    value = os.environ.get(
        "CARD_SCANNER_POSTGRES_EXPECTED_DATABASE",
        "cardscanner_dev",
    ).strip()

    if not value:
        raise RuntimeError(
            "CARD_SCANNER_POSTGRES_EXPECTED_DATABASE cannot be empty"
        )

    return value


def initialize_postgres_worker() -> dict[str, object]:
    """Open the pool, verify the target, and apply pending schema migrations."""
    expected = expected_database_name()

    try:
        # This function is called from Gunicorn's post_fork hook, never from
        # the preloaded master process.
        open_pool()

        with connection() as conn:
            row = conn.execute(
                "SELECT current_database() AS database"
            ).fetchone()

        actual = row["database"] if row else None

        # Crucially, do this before run_migrations() or any schema writes.
        if actual != expected:
            raise RuntimeError(
                "PostgreSQL startup safety check failed: "
                f"expected database {expected!r}, connected to {actual!r}"
            )

        applied = run_migrations()

        with connection() as conn:
            rows = conn.execute(
                """
                SELECT schema_name
                FROM information_schema.schemata
                WHERE schema_name IN ('app', 'catalog')
                """
            ).fetchall()

        schemas = {row["schema_name"] for row in rows}
        missing = {"app", "catalog"} - schemas

        if missing:
            raise RuntimeError(
                "PostgreSQL startup failed; required schemas are missing: "
                + ", ".join(sorted(missing))
            )

        return {
            "database": actual,
            "schemas": sorted(schemas),
            "applied_migrations": applied,
        }

    except BaseException:
        close_pool()
        raise


def shutdown_postgres_worker() -> None:
    """Close the current Gunicorn worker's PostgreSQL pool."""
    close_pool()
