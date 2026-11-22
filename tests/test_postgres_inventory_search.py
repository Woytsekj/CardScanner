"""Integration tests for PostgreSQL inventory autocomplete."""

from __future__ import annotations

import os
import unittest
from uuid import uuid4

from postgres_app_db import connect_app_database
from postgres_db import close_pool, open_pool, run_migrations
from postgres_inventory_search import fetch_inventory_search_suggestions


@unittest.skipUnless(
    os.environ.get("CARD_SCANNER_POSTGRES_DSN"),
    "CARD_SCANNER_POSTGRES_DSN is not configured",
)
class PostgreSQLInventorySearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        open_pool()
        run_migrations()

    @classmethod
    def tearDownClass(cls):
        close_pool()

    def setUp(self):
        token = uuid4().hex
        self.term = f"PGSearch{token}"
        self.scryfall_id = f"pg-inventory-search-{token}"
        self.alias_term = f"aliasneedle{token}"
        self.fallback_term = f"PGFallback{token}"
        self.fallback_scryfall_id = "0000579f-7b35-4ed3-b44c-db2a538066fe"
        self.inventory_ids = []

        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            self.add_inventory(
                conn, self.scryfall_id, f"{self.term} Card",
                "ZZT", "TEST-7", 2, "TestOwner",
            )
            self.add_inventory(
                conn, self.scryfall_id, f"{self.term} Card",
                "ZZT", "TEST-7", 3, "TestOwner",
            )
            self.add_inventory(
                conn, self.scryfall_id, f"{self.term} Card",
                "ZZT", "TEST-7", 7, "OtherOwner",
            )

            self.add_alias(conn, self.scryfall_id, f"{self.alias_term} preferred")
            self.add_alias(conn, self.scryfall_id, f"inside {self.alias_term} alternate")

            # This real catalog printing has collector number 157. The test
            # inventory row intentionally omits collector_number so the
            # normalized printing join must supply it.
            self.add_inventory(
                conn,
                self.fallback_scryfall_id,
                self.fallback_term,
                "tsp",
                None,
                1,
                "TestOwner",
            )

            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _insert_inventory_row(
        conn,
        scryfall_id,
        name,
        set_code,
        collector_number,
        quantity,
        owner,
    ):
        row = conn.execute(
            """
            INSERT INTO app.inventory (
                game_id, scryfall_id, name, set_code,
                collector_number, quantity, owner
            )
            VALUES ('mtg', ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (
                scryfall_id,
                name,
                set_code,
                collector_number,
                quantity,
                owner,
            ),
        ).fetchone()
        return row["id"]

    def add_inventory(self, conn, *args):
        row_id = self._insert_inventory_row(conn, *args)
        self.inventory_ids.append(row_id)

    def add_alias(self, conn, scryfall_id, alias):
        conn.execute(
            """
            INSERT INTO app.card_aliases (
                game_id, provider_id, external_printing_id, alias
            )
            VALUES ('mtg', 'scryfall', ?, ?)
            """,
            (scryfall_id, alias),
        )

    def tearDown(self):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            conn.execute(
                """
                DELETE FROM app.card_aliases
                WHERE game_id = 'mtg'
                  AND provider_id = 'scryfall'
                  AND external_printing_id = ?
                """,
                (self.scryfall_id,),
            )

            for row_id in self.inventory_ids:
                conn.execute(
                    "DELETE FROM app.inventory WHERE id = ?",
                    (row_id,),
                )

            for sid in (self.scryfall_id, self.fallback_scryfall_id):
                conn.execute(
                    "DELETE FROM app.inventory WHERE id IN ("
                    "SELECT id FROM app.inventory "
                    "WHERE game_id = 'mtg' AND scryfall_id = ? "
                    "AND name = ?"
                    ")",
                    (
                        sid,
                        self.fallback_term if sid == self.fallback_scryfall_id
                        else f"{self.term} Card",
                    ),
                )

            conn.commit()
        finally:
            conn.close()

    def search(self, query, owner=None):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            return fetch_inventory_search_suggestions(
                conn,
                query,
                owner=owner,
                limit=12,
            )
        finally:
            conn.close()

    def test_owner_filter_is_case_insensitive_and_applied_before_sum(self):
        all_results = self.search(self.term)
        self.assertEqual(len(all_results), 1)
        self.assertEqual(all_results[0]["quantity"], 12)

        owner_results = self.search(self.term, owner="testowner")
        self.assertEqual(len(owner_results), 1)
        self.assertEqual(owner_results[0]["quantity"], 5)
        self.assertEqual(owner_results[0]["name"], f"{self.term} Card")

    def test_alias_search_prefers_prefix_match_and_lists_all_aliases(self):
        results = self.search(self.alias_term)

        self.assertEqual(len(results), 1)
        card = results[0]

        self.assertEqual(
            card["matched_alias"],
            f"{self.alias_term} preferred",
        )
        self.assertIn(f"{self.alias_term} preferred", card["alternate_names"])
        self.assertIn(
            f"inside {self.alias_term} alternate",
            card["alternate_names"],
        )

    def test_collector_number_falls_back_to_normalized_catalog(self):
        results = self.search(self.fallback_term)

        self.assertEqual(len(results), 1)
        self.assertEqual(
            results[0]["scryfall_id"],
            self.fallback_scryfall_id,
        )
        self.assertEqual(results[0]["collector_number"], "157")

    def test_short_queries_return_empty_results(self):
        self.assertEqual(self.search(""), [])
        self.assertEqual(self.search("x"), [])
        self.assertEqual(self.search("  "), [])


@unittest.skipUnless(
    os.environ.get("CARD_SCANNER_POSTGRES_DSN")
    and os.environ.get("CARD_SCANNER_DATABASE_BACKEND") == "postgres",
    "Run with the PostgreSQL backend and development DSN configured",
)
class PostgreSQLInventorySearchHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        open_pool()
        run_migrations()

        import app as app_module

        if app_module.DATABASE_BACKEND != "postgres":
            raise RuntimeError(
                "Inventory search HTTP tests require PostgreSQL app mode"
            )

        cls.app_module = app_module
        cls.app_module.app.config["TESTING"] = True

    @classmethod
    def tearDownClass(cls):
        close_pool()

    def setUp(self):
        token = uuid4().hex
        self.admin_username = f"pg-search-admin-{token}"
        self.user_username = f"pg-search-user-{token}"

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

    def test_admin_can_use_inventory_autocomplete(self):
        response = self.admin_client.get(
            "/search_suggest?q=Mountain&owner=Jonathan",
            base_url="http://localhost",
        )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertIsInstance(payload["results"], list)
        self.assertLessEqual(len(payload["results"]), 12)

        for item in payload["results"]:
            self.assertEqual(
                set(item),
                {
                    "scryfall_id",
                    "name",
                    "set_code",
                    "collector_number",
                    "quantity",
                    "alternate_names",
                    "matched_alias",
                    "image_url",
                },
            )

    def test_guest_is_redirected_to_login(self):
        response = self.guest_client.get(
            "/search_suggest?q=Mountain",
            base_url="http://localhost",
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.location)

    def test_user_inventory_search_is_scoped_to_their_collection(self):
        response = self.user_client.get(
            "/search_suggest?q=Mountain",
            base_url="http://localhost",
        )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["results"], [])


if __name__ == "__main__":
    unittest.main()
