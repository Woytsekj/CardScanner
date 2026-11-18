"""Integration tests for PostgreSQL card information queries."""

from __future__ import annotations

import os
import unittest
from uuid import uuid4

from postgres_app_db import connect_app_database
from postgres_card_info import fetch_card_info
from postgres_db import close_pool, open_pool, run_migrations


@unittest.skipUnless(
    os.environ.get("CARD_SCANNER_POSTGRES_DSN"),
    "CARD_SCANNER_POSTGRES_DSN is not configured",
)
class PostgreSQLCardInfoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        open_pool()
        run_migrations()

    @classmethod
    def tearDownClass(cls):
        close_pool()

    def fetch(self, scryfall_id):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            return fetch_card_info(conn, scryfall_id)
        finally:
            conn.close()

    def test_two_face_card_returns_metadata_and_both_tag_groups(self):
        # This migrated printing was confirmed in the development catalog.
        scryfall_id = "fe675f03-cbb5-4177-b7d7-64a30260ee2a"
        card = self.fetch(scryfall_id)

        self.assertIsNotNone(card)
        self.assertEqual(card["scryfall_id"], scryfall_id)
        self.assertEqual(
            card["name"],
            "Archangel Avacyn // Avacyn, the Purifier",
        )
        self.assertIsInstance(card["legalities"], dict)
        self.assertIsInstance(card["updated_at"], str)
        self.assertTrue(card["updated_at"])

        self.assertTrue(card["art_tags"])
        self.assertTrue(card["oracle_tags"])

        for key in ("art_tags", "oracle_tags"):
            tags = card[key]
            self.assertLessEqual(len(tags), 100)
            self.assertEqual(len(tags), len(set(tags)))
            self.assertEqual(
                tags,
                sorted(tags, key=lambda value: (value.casefold(), value)),
            )

    def test_unknown_printing_returns_none(self):
        self.assertIsNone(
            self.fetch("00000000-0000-0000-0000-000000000000")
        )



@unittest.skipUnless(
    os.environ.get("CARD_SCANNER_POSTGRES_DSN")
    and os.environ.get("CARD_SCANNER_DATABASE_BACKEND") == "postgres",
    "Run with the PostgreSQL backend and development DSN configured",
)
class PostgreSQLCardInfoHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        open_pool()
        run_migrations()

        import app as app_module

        if app_module.DATABASE_BACKEND != "postgres":
            raise RuntimeError("HTTP tests require PostgreSQL app mode")

        cls.app_module = app_module

    @classmethod
    def tearDownClass(cls):
        close_pool()

    def setUp(self):
        from uuid import uuid4

        self.token = uuid4().hex
        self.admin_username = f"pg-cardinfo-admin-{self.token}"
        self.user_username = f"pg-cardinfo-user-{self.token}"

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

    def test_admin_receives_card_info_over_http(self):
        scryfall_id = "fe675f03-cbb5-4177-b7d7-64a30260ee2a"

        response = self.admin_client.get(
            f"/api/card-info/{scryfall_id}",
            base_url="http://localhost",
        )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = response.get_json()
        self.assertTrue(payload["success"])

        card = payload["card"]
        self.assertEqual(card["scryfall_id"], scryfall_id)
        self.assertEqual(
            card["name"],
            "Archangel Avacyn // Avacyn, the Purifier",
        )
        self.assertIsInstance(card["legalities"], dict)
        self.assertTrue(card["art_tags"])
        self.assertTrue(card["oracle_tags"])

    def test_admin_receives_404_for_missing_card(self):
        response = self.admin_client.get(
            "/api/card-info/00000000-0000-0000-0000-000000000000",
            base_url="http://localhost",
        )

        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.get_json()["success"])

    def test_unauthenticated_visitor_receives_401(self):
        response = self.guest_client.get(
            "/api/card-info/fe675f03-cbb5-4177-b7d7-64a30260ee2a",
            base_url="http://localhost",
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error"], "Login required")

    def test_non_admin_user_receives_403(self):
        response = self.user_client.get(
            "/api/card-info/fe675f03-cbb5-4177-b7d7-64a30260ee2a",
            base_url="http://localhost",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"], "Admin access required")


if __name__ == "__main__":
    unittest.main()
