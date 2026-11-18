"""Integration tests for PostgreSQL manual-add catalog search."""

from __future__ import annotations

import os
import unittest
from uuid import uuid4

from postgres_app_db import connect_app_database
from postgres_catalog_search import fetch_catalog_search_results
from postgres_db import close_pool, open_pool, run_migrations


@unittest.skipUnless(
    os.environ.get("CARD_SCANNER_POSTGRES_DSN"),
    "CARD_SCANNER_POSTGRES_DSN is not configured",
)
class PostgreSQLCatalogSearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        open_pool()
        run_migrations()

    @classmethod
    def tearDownClass(cls):
        close_pool()

    def search(self, query, limit=12):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            return fetch_catalog_search_results(conn, query, limit=limit)
        finally:
            conn.close()

    def test_finds_printing_whose_name_falls_back_to_metadata(self):
        # This printing has no catalog.cards row. Its metadata name and face
        # name were both verified against the archived SQLite card_art table.
        scryfall_id = "004c1078-0dea-4885-97b5-497f3f15c8c3"

        results = self.search("A-Falcon Abomination", limit=40)
        card = next(
            (item for item in results if item["scryfall_id"] == scryfall_id),
            None,
        )

        self.assertIsNotNone(card)
        self.assertEqual(card["name"], "A-Falcon Abomination")
        self.assertTrue(card["set_code"])
        self.assertTrue(card["collector_number"])
        self.assertEqual(
            card["image_url"],
            "https://cards.scryfall.io/normal/front/0/0/"
            "004c1078-0dea-4885-97b5-497f3f15c8c3.jpg",
        )
        self.assertEqual(
            set(card),
            {
                "scryfall_id",
                "name",
                "set_code",
                "rarity",
                "tcgplayer_id",
                "collector_number",
                "image_url",
            },
        )

    def test_blank_search_returns_no_results(self):
        self.assertEqual(self.search(""), [])
        self.assertEqual(self.search("   "), [])

    def test_result_limit_is_clamped(self):
        results = self.search("Forest", limit=1000)
        self.assertLessEqual(len(results), 40)

        one_result = self.search("Forest", limit=0)
        self.assertLessEqual(len(one_result), 1)

    def test_numeric_tcgcsv_product_ids_are_returned_as_integers(self):
        # This migrated mapping was compared against SQLite: the Scryfall
        # printing has TCGplayer product ID 558404.
        results = self.search("Fury Sliver", limit=40)
        card = next(
            (
                item for item in results
                if item["scryfall_id"] == "0000579f-7b35-4ed3-b44c-db2a538066fe"
            ),
            None,
        )

        self.assertIsNotNone(card)
        self.assertEqual(card["tcgplayer_id"], 14240)
        self.assertIsInstance(card["tcgplayer_id"], int)


@unittest.skipUnless(
    os.environ.get("CARD_SCANNER_POSTGRES_DSN")
    and os.environ.get("CARD_SCANNER_DATABASE_BACKEND") == "postgres",
    "Run with the PostgreSQL backend and development DSN configured",
)
class PostgreSQLCatalogSearchHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        open_pool()
        run_migrations()

        import app as app_module

        if app_module.DATABASE_BACKEND != "postgres":
            raise RuntimeError("Catalog search HTTP tests require PostgreSQL app mode")

        cls.app_module = app_module
        cls.app_module.app.config["TESTING"] = True

    @classmethod
    def tearDownClass(cls):
        close_pool()

    def setUp(self):
        token = uuid4().hex
        self.admin_username = f"pg-catalog-admin-{token}"
        self.user_username = f"pg-catalog-user-{token}"

        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            conn.execute(
                """
                INSERT INTO app.users
                    (username, password_hash, is_admin, role,
                     is_superadmin, active)
                VALUES (?, ?, TRUE, 'admin', FALSE, TRUE)
                """,
                (self.admin_username, "unused-test-password-hash"),
            )
            conn.execute(
                """
                INSERT INTO app.users
                    (username, password_hash, is_admin, role,
                     is_superadmin, active)
                VALUES (?, ?, FALSE, 'user', FALSE, TRUE)
                """,
                (self.user_username, "unused-test-password-hash"),
            )
            conn.commit()
        finally:
            conn.close()

        self.admin_client = self.app_module.app.test_client()
        self.user_client = self.app_module.app.test_client()
        self.guest_client = self.app_module.app.test_client()

        with self.admin_client.session_transaction() as session:
            session["username"] = self.admin_username

        with self.user_client.session_transaction() as session:
            session["username"] = self.user_username

    def tearDown(self):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            conn.execute(
                "DELETE FROM app.users WHERE username IN (?, ?)",
                (self.admin_username, self.user_username),
            )
            conn.commit()
        finally:
            conn.close()

    def test_admin_can_search_catalog(self):
        response = self.admin_client.get(
            "/admin/catalog_search?q=A-Falcon%20Abomination",
            base_url="http://localhost",
        )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertTrue(
            any(
                item["scryfall_id"] == "004c1078-0dea-4885-97b5-497f3f15c8c3"
                for item in payload["results"]
            )
        )
        self.assertLessEqual(len(payload["results"]), 40)

    def test_admin_can_use_catalog_autocomplete(self):
        response = self.admin_client.get(
            "/admin/catalog_suggest?q=A-Fa",
            base_url="http://localhost",
        )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertLessEqual(len(payload["results"]), 10)
        self.assertTrue(
            any(
                item["scryfall_id"] == "004c1078-0dea-4885-97b5-497f3f15c8c3"
                for item in payload["results"]
            )
        )

    def test_short_autocomplete_query_returns_empty_results(self):
        response = self.admin_client.get(
            "/admin/catalog_suggest?q=A",
            base_url="http://localhost",
        )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json()["results"], [])

    def test_both_catalog_routes_remain_admin_only(self):
        paths = (
            "/admin/catalog_search?q=Forest",
            "/admin/catalog_suggest?q=Fo",
        )

        for path in paths:
            with self.subTest(path=path, user="guest"):
                response = self.guest_client.get(
                    path,
                    base_url="http://localhost",
                )
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.location.startswith("/login"))

            with self.subTest(path=path, user="non-admin"):
                response = self.user_client.get(
                    path,
                    base_url="http://localhost",
                )
                self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
