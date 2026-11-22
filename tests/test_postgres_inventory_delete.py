"""PostgreSQL integration tests for single-row inventory deletion."""

from __future__ import annotations

import os
import unittest
from uuid import uuid4

from postgres_app_db import connect_app_database
from postgres_db import close_pool, open_pool, run_migrations


@unittest.skipUnless(
    os.environ.get("CARD_SCANNER_POSTGRES_DSN")
    and os.environ.get("CARD_SCANNER_DATABASE_BACKEND") == "postgres",
    "Run with the PostgreSQL backend and development DSN configured",
)
class PostgreSQLInventoryDeleteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        open_pool()
        run_migrations()

        import app as app_module

        if app_module.DATABASE_BACKEND != "postgres":
            raise RuntimeError("Inventory deletion tests require PostgreSQL app mode")

        cls.app_module = app_module
        cls.app_module.app.config["TESTING"] = True

    @classmethod
    def tearDownClass(cls):
        close_pool()

    def setUp(self):
        self.token = uuid4().hex
        self.owner_name = f"PG Inventory Delete Owner {self.token}"
        self.card_name = f"Inventory Delete Card {self.token}"
        self.admin_username = f"pg-invdelete-admin-{self.token}"
        self.vendor_username = f"pg-invdelete-vendor-{self.token}"
        self.csrf = f"csrf-invdelete-{self.token}"

        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            person = conn.execute(
                """
                INSERT INTO app.people (name, active)
                VALUES (?, TRUE)
                RETURNING id
                """,
                (self.owner_name,),
            ).fetchone()
            self.person_id = int(person["id"])

            conn.execute(
                """
                INSERT INTO app.users (
                    username, password_hash, is_admin, role,
                    is_superadmin, active, person_id
                )
                VALUES (?, ?, TRUE, 'admin', FALSE, TRUE, ?)
                """,
                (self.admin_username, "unused-inventory-delete-admin-hash", self.person_id),
            )
            conn.execute(
                """
                INSERT INTO app.users (
                    username, password_hash, is_admin, role,
                    is_superadmin, active
                )
                VALUES (?, ?, FALSE, 'vendor', FALSE, TRUE)
                """,
                (self.vendor_username, "unused-inventory-delete-vendor-hash"),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        self.admin_client = self.app_module.app.test_client()
        self.vendor_client = self.app_module.app.test_client()

        with self.admin_client.session_transaction() as session:
            session["username"] = self.admin_username
            session["csrf_token"] = self.csrf

        with self.vendor_client.session_transaction() as session:
            session["username"] = self.vendor_username
            session["csrf_token"] = self.csrf

    def tearDown(self):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            conn.execute(
                "DELETE FROM app.inventory WHERE owner = ?",
                (self.owner_name,),
            )
            conn.execute(
                "DELETE FROM app.users WHERE username IN (?, ?)",
                (self.admin_username, self.vendor_username),
            )
            conn.execute(
                "DELETE FROM app.people WHERE id = ?",
                (self.person_id,),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def add_inventory(self, *, game_id="mtg") -> int:
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            row = conn.execute(
                """
                INSERT INTO app.inventory (
                    game_id, name, owner, person_id, quantity
                )
                VALUES (?, ?, ?, ?, 2)
                RETURNING id
                """,
                (game_id, self.card_name, self.owner_name, self.person_id),
            ).fetchone()
            conn.commit()
            return int(row["id"])
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def fetch_inventory(self, item_id: int):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            return conn.execute(
                """
                SELECT id, game_id, name, owner
                FROM app.inventory
                WHERE id = ?
                """,
                (item_id,),
            ).fetchone()
        finally:
            conn.close()

    def delete_request(self, item_id: int, *, client=None, csrf=True):
        kwargs = {"base_url": "http://localhost"}
        if csrf:
            kwargs["headers"] = {"X-CSRFToken": self.csrf}
        return (client or self.admin_client).delete(
            f"/delete/{item_id}",
            **kwargs,
        )

    def test_admin_can_delete_inventory_row(self):
        item_id = self.add_inventory()

        response = self.delete_request(item_id)

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json(), {"success": True})
        self.assertIsNone(self.fetch_inventory(item_id))

    def test_deleting_missing_row_returns_success_false(self):
        item_id = self.add_inventory()

        first = self.delete_request(item_id)
        second = self.delete_request(item_id)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.get_json(), {"success": True})
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.get_json(), {"success": False})
        self.assertIsNone(self.fetch_inventory(item_id))

    def test_delete_requires_csrf(self):
        item_id = self.add_inventory()

        response = self.delete_request(item_id, csrf=False)

        self.assertEqual(response.status_code, 400)
        self.assertIsNotNone(self.fetch_inventory(item_id))

    def test_vendor_cannot_delete_inventory(self):
        item_id = self.add_inventory()

        response = self.delete_request(item_id, client=self.vendor_client)

        self.assertEqual(response.status_code, 403)
        self.assertIsNotNone(self.fetch_inventory(item_id))

    def test_delete_does_not_remove_inventory_from_another_game(self):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            other_game = conn.execute(
                """
                SELECT game_id
                FROM catalog.games
                WHERE game_id <> 'mtg'
                ORDER BY game_id
                LIMIT 1
                """
            ).fetchone()
        finally:
            conn.close()

        if not other_game:
            self.skipTest("No non-MTG game is seeded in the development database.")

        item_id = self.add_inventory(game_id=other_game["game_id"])

        response = self.delete_request(item_id)

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json(), {"success": False})

        remaining = self.fetch_inventory(item_id)
        self.assertIsNotNone(remaining)
        self.assertEqual(remaining["game_id"], other_game["game_id"])
