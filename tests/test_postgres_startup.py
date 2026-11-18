"""Tests for guarded, post-fork PostgreSQL application startup."""
from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

import postgres_app_startup as startup


class PostgreSQLStartupSafetyTests(unittest.TestCase):
    def test_wrong_database_is_rejected_before_migrations(self):
        fake_conn = MagicMock()
        fake_conn.execute.return_value.fetchone.return_value = {
            "database": "not_cardscanner_dev"
        }

        fake_context = MagicMock()
        fake_context.__enter__.return_value = fake_conn
        fake_context.__exit__.return_value = False

        with (
            patch.dict(
                os.environ,
                {"CARD_SCANNER_POSTGRES_EXPECTED_DATABASE": "cardscanner_dev"},
            ),
            patch.object(startup, "open_pool") as open_pool,
            patch.object(startup, "connection", return_value=fake_context),
            patch.object(startup, "run_migrations") as run_migrations,
            patch.object(startup, "close_pool") as close_pool,
        ):
            with self.assertRaisesRegex(
                RuntimeError, "startup safety check failed"
            ):
                startup.initialize_postgres_worker()

        open_pool.assert_called_once()
        run_migrations.assert_not_called()
        close_pool.assert_called_once()

    def test_empty_expected_database_is_rejected(self):
        with patch.dict(
            os.environ,
            {"CARD_SCANNER_POSTGRES_EXPECTED_DATABASE": ""},
        ):
            with self.assertRaisesRegex(RuntimeError, "cannot be empty"):
                startup.expected_database_name()


@unittest.skipUnless(
    os.environ.get("CARD_SCANNER_POSTGRES_DSN"),
    "CARD_SCANNER_POSTGRES_DSN is not configured",
)
class PostgreSQLStartupIntegrationTests(unittest.TestCase):
    def test_worker_startup_verifies_development_database(self):
        from postgres_db import close_pool, connection

        try:
            result = startup.initialize_postgres_worker()

            self.assertEqual(result["database"], "cardscanner_dev")
            self.assertEqual(set(result["schemas"]), {"app", "catalog"})

            with connection() as conn:
                row = conn.execute(
                    "SELECT current_database() AS database"
                ).fetchone()

            self.assertEqual(row["database"], "cardscanner_dev")
        finally:
            close_pool()


if __name__ == "__main__":
    unittest.main()
