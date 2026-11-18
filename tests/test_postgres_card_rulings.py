"""PostgreSQL integration tests for the Scryfall rulings endpoint."""

from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch
from uuid import uuid4

from postgres_app_db import connect_app_database
from postgres_db import close_pool, open_pool, run_migrations


@unittest.skipUnless(
    os.environ.get("CARD_SCANNER_POSTGRES_DSN")
    and os.environ.get("CARD_SCANNER_DATABASE_BACKEND") == "postgres",
    "Run with the PostgreSQL backend and development DSN configured",
)
class PostgreSQLCardRulingsHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        open_pool()
        run_migrations()

        import app as app_module

        if app_module.DATABASE_BACKEND != "postgres":
            raise RuntimeError("Rulings HTTP tests require PostgreSQL app mode")

        cls.app_module = app_module

    @classmethod
    def tearDownClass(cls):
        close_pool()

    def setUp(self):
        self.scryfall_id = str(uuid4())
        self.admin_username = f"pg-rulings-admin-{uuid4().hex}"
        self.user_username = f"pg-rulings-user-{uuid4().hex}"

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
                """
                DELETE FROM catalog.printing_rulings_cache
                WHERE game_id='mtg'
                  AND provider_id='scryfall'
                  AND external_printing_id=?
                """,
                (self.scryfall_id,),
            )
            conn.execute(
                "DELETE FROM app.users WHERE username IN (?, ?)",
                (self.admin_username, self.user_username),
            )
            conn.commit()
        finally:
            conn.close()

    def get_rulings(self, client=None):
        client = client or self.admin_client
        return client.get(
            f"/api/card-rulings/{self.scryfall_id}",
            base_url="http://localhost",
        )

    @staticmethod
    def scryfall_response(rows):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"object": "list", "data": rows}
        return response

    def read_cache(self):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            return conn.execute(
                """
                SELECT rulings::text AS rulings_json, fetched_at, printing_id
                FROM catalog.printing_rulings_cache
                WHERE game_id='mtg'
                  AND provider_id='scryfall'
                  AND external_printing_id=?
                """,
                (self.scryfall_id,),
            ).fetchone()
        finally:
            conn.close()

    def test_fetches_normalizes_saves_and_reuses_fresh_cache(self):
        rows = [
            {
                "published_at": "2019-06-14",
                "source": "WOTC",
                "comment": "Older official ruling.",
            },
            {
                "published_at": "2020-01-02",
                "source": "Scryfall",
                "comment": " Newer Scryfall note. ",
            },
            {"published_at": "2021-01-01", "source": "wotc", "comment": "   "},
            None,
        ]

        with patch.object(
            self.app_module.requests,
            "get",
            return_value=self.scryfall_response(rows),
        ) as get:
            first = self.get_rulings()
            self.assertEqual(first.status_code, 200, first.get_data(as_text=True))

            payload = first.get_json()
            self.assertFalse(payload["cached"])
            self.assertFalse(payload["stale"])
            self.assertEqual(
                [item["published_at"] for item in payload["rulings"]],
                ["2020-01-02", "2019-06-14"],
            )
            self.assertEqual(
                payload["rulings"][0]["source"],
                "scryfall",
            )
            self.assertEqual(
                payload["rulings"][0]["comment"],
                "Newer Scryfall note.",
            )

            saved = self.read_cache()
            self.assertIsNotNone(saved)
            self.assertIsNotNone(saved["fetched_at"])
            self.assertEqual(
                json.loads(saved["rulings_json"]),
                payload["rulings"],
            )

            second = self.get_rulings()
            self.assertEqual(second.status_code, 200)
            self.assertTrue(second.get_json()["cached"])
            self.assertEqual(
                second.get_json()["rulings"],
                payload["rulings"],
            )
            get.assert_called_once()

    def test_empty_rulings_are_cached(self):
        with patch.object(
            self.app_module.requests,
            "get",
            return_value=self.scryfall_response([]),
        ) as get:
            first = self.get_rulings()
            second = self.get_rulings()

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.get_json()["rulings"], [])
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.get_json()["cached"])
        self.assertEqual(second.get_json()["rulings"], [])
        get.assert_called_once()

    def test_stale_cache_is_returned_when_refresh_fails(self):
        initial_rows = [
            {
                "published_at": "2019-06-14",
                "source": "wotc",
                "comment": "Saved official ruling.",
            }
        ]

        with patch.object(
            self.app_module.requests,
            "get",
            return_value=self.scryfall_response(initial_rows),
        ):
            initial = self.get_rulings()

        self.assertEqual(initial.status_code, 200)

        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            conn.execute(
                """
                UPDATE catalog.printing_rulings_cache
                SET fetched_at=?
                WHERE game_id='mtg'
                  AND provider_id='scryfall'
                  AND external_printing_id=?
                """,
                (datetime(2020, 1, 1, tzinfo=timezone.utc), self.scryfall_id),
            )
            conn.commit()
        finally:
            conn.close()

        with patch.object(
            self.app_module.requests,
            "get",
            side_effect=self.app_module.requests.Timeout("offline"),
        ):
            response = self.get_rulings()

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = response.get_json()
        self.assertTrue(payload["stale"])
        self.assertTrue(payload["cached"])
        self.assertIn("refreshing from Scryfall failed", payload["warning"])
        self.assertEqual(
            payload["rulings"][0]["comment"],
            "Saved official ruling.",
        )

    def test_upstream_failure_without_cache_returns_502(self):
        with patch.object(
            self.app_module.requests,
            "get",
            side_effect=self.app_module.requests.Timeout("offline"),
        ):
            response = self.get_rulings()

        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.get_json()["success"])

    def test_invalid_card_id_returns_400_without_network_request(self):
        with patch.object(self.app_module.requests, "get") as get:
            response = self.admin_client.get(
                "/api/card-rulings/not-a-card-id",
                base_url="http://localhost",
            )

        self.assertEqual(response.status_code, 400)
        get.assert_not_called()

    def test_unauthenticated_visitor_receives_401(self):
        response = self.get_rulings(self.guest_client)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error"], "Login required")

    def test_non_admin_user_receives_403(self):
        response = self.get_rulings(self.user_client)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"], "Admin access required")


if __name__ == "__main__":
    unittest.main()
