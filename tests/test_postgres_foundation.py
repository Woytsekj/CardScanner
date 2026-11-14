"""Integration tests for the isolated PostgreSQL foundation.

Run against a disposable development/test database with
CARD_SCANNER_POSTGRES_DSN configured. These tests never touch SQLite.
"""
from __future__ import annotations

import os
import unittest

from postgres_db import close_pool, open_pool, run_migrations


@unittest.skipUnless(
    os.environ.get("CARD_SCANNER_POSTGRES_DSN"),
    "CARD_SCANNER_POSTGRES_DSN is not configured",
)
class PostgreSQLFoundationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        open_pool()
        run_migrations()

    @classmethod
    def tearDownClass(cls):
        close_pool()

    def test_initial_games_and_tcgcsv_categories(self):
        from postgres_db import connection

        with connection() as conn:
            rows = conn.execute(
                """
                SELECT g.game_id, gp.provider_category_id
                FROM catalog.games AS g
                JOIN catalog.game_providers AS gp USING (game_id)
                WHERE gp.provider_id = 'tcgcsv'
                ORDER BY g.game_id
                """
            ).fetchall()

        self.assertEqual(
            rows,
            [
                {"game_id": "mtg", "provider_category_id": "1"},
                {"game_id": "one_piece", "provider_category_id": "68"},
                {"game_id": "pokemon", "provider_category_id": "3"},
                {"game_id": "riftbound", "provider_category_id": "89"},
            ],
        )

    def test_migrations_are_idempotent(self):
        self.assertEqual(run_migrations(), [])

    def test_scryfall_is_only_registered_for_mtg(self):
        from postgres_db import connection

        with connection() as conn:
            rows = conn.execute(
                "SELECT game_id FROM catalog.game_providers WHERE provider_id = 'scryfall'"
            ).fetchall()

        self.assertEqual(rows, [{"game_id": "mtg"}])


if __name__ == "__main__":
    unittest.main()
