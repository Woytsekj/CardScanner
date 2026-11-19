"""Seed deterministic, synthetic fixtures into the isolated PostgreSQL CI database."""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from postgres_db import close_pool, connection, open_pool, run_migrations


def main() -> None:
    expected = os.environ.get("CARD_SCANNER_POSTGRES_EXPECTED_DATABASE", "").strip()
    if expected != "cardscanner_dev":
        raise RuntimeError(
            "Refusing to seed PostgreSQL CI fixtures unless the expected database "
            "is exactly 'cardscanner_dev'."
        )

    if os.environ.get("CARD_SCANNER_DATABASE_BACKEND", "").strip().lower() != "postgres":
        raise RuntimeError(
            "Refusing to seed PostgreSQL CI fixtures unless PostgreSQL app mode is enabled."
        )

    open_pool()
    try:
        run_migrations()
        sql_path = Path(__file__).parent / "fixtures" / "postgres_ci_seed.sql"
        sql = sql_path.read_text(encoding="utf-8")
        with connection() as conn:
            row = conn.execute(
                "SELECT current_database() AS database"
            ).fetchone()
            if row["database"] != expected:
                raise RuntimeError(
                    f"Refusing to seed {row['database']!r}; expected {expected!r}."
                )
            conn.execute(sql, prepare=False)
    finally:
        close_pool()


if __name__ == "__main__":
    main()
