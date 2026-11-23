"""PostgreSQL integration tests for single-row inventory updates."""

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
class PostgreSQLInventoryUpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        open_pool()
        run_migrations()

        import app as app_module

        if app_module.DATABASE_BACKEND != "postgres":
            raise RuntimeError("Inventory update tests require PostgreSQL app mode")

        cls.app_module = app_module
        cls.app_module.app.config["TESTING"] = True

    @classmethod
    def tearDownClass(cls):
        close_pool()

    def setUp(self):
        self.token = uuid4().hex
        self.owner_name = f"PG Inventory Update Owner {self.token}"
        self.target_name = f"PG Inventory Update Target {self.token}"
        self.inactive_name = f"PG Inventory Update Inactive {self.token}"
        self.admin_username = f"pg-invupdate-admin-{self.token}"
        self.vendor_username = f"pg-invupdate-vendor-{self.token}"
        self.csrf = f"csrf-invupdate-{self.token}"
        self.card_name = f"Inventory Update Card {self.token}"
        self.deck_ids = []

        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            people = []
            for name, active in (
                (self.owner_name, True),
                (self.target_name, True),
                (self.inactive_name, False),
            ):
                row = conn.execute(
                    """
                    INSERT INTO app.people (name, active)
                    VALUES (?, ?)
                    RETURNING id
                    """,
                    (name, active),
                ).fetchone()
                people.append(int(row["id"]))

            self.owner_person_id, self.target_person_id, self.inactive_person_id = people

            conn.execute(
                """
                INSERT INTO app.users (
                    username, password_hash, is_admin, role,
                    is_superadmin, active, person_id
                )
                VALUES (?, ?, TRUE, 'admin', FALSE, TRUE, ?)
                """,
                (self.admin_username, "unused-inventory-update-admin-hash", self.owner_person_id),
            )
            conn.execute(
                """
                INSERT INTO app.users (
                    username, password_hash, is_admin, role,
                    is_superadmin, active, person_id, manager_username
                )
                VALUES (?, ?, FALSE, 'user', FALSE, TRUE, ?, ?)
                """,
                (
                    f"pg-invupdate-managed-{self.token}",
                    "unused-inventory-update-managed-hash",
                    self.target_person_id,
                    self.admin_username,
                ),
            )
            conn.execute(
                """
                INSERT INTO app.users (
                    username, password_hash, is_admin, role,
                    is_superadmin, active
                )
                VALUES (?, ?, FALSE, 'vendor', FALSE, TRUE)
                """,
                (self.vendor_username, "unused-inventory-update-vendor-hash"),
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
                """
                DELETE FROM app.inventory
                WHERE owner IN (?, ?, ?)
                """,
                (self.owner_name, self.target_name, self.inactive_name),
            )

            for deck_id in self.deck_ids:
                conn.execute(
                    "DELETE FROM app.decklists WHERE id = ?",
                    (deck_id,),
                )

            conn.execute(
                "DELETE FROM app.users WHERE username IN (?, ?, ?)",
                (
                    self.admin_username,
                    self.vendor_username,
                    f"pg-invupdate-managed-{self.token}",
                ),
            )
            conn.execute(
                "DELETE FROM app.people WHERE id IN (?, ?, ?)",
                (
                    self.owner_person_id,
                    self.target_person_id,
                    self.inactive_person_id,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def add_deck(self, name: str | None = None) -> int:
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            row = conn.execute(
                """
                INSERT INTO app.decklists (
                    game_id, name, source, created_at, updated_at, tags_json
                )
                VALUES ('mtg', ?, 'manual', now(), now(), '[]'::jsonb)
                RETURNING id
                """,
                (name or f"PG Inventory Update Deck {self.token}",),
            ).fetchone()
            conn.commit()
            deck_id = int(row["id"])
            self.deck_ids.append(deck_id)
            return deck_id
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def add_inventory(
        self,
        *,
        owner: str | None = None,
        person_id: int | None = None,
        location: str = "Binder 1",
        finish: str = "normal",
        quantity: int = 2,
        deck_id: int | None = None,
        for_vending: bool = False,
    ) -> int:
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            row = conn.execute(
                """
                INSERT INTO app.inventory (
                    game_id, name, owner, person_id, quantity,
                    finish, location, deck_id, for_vending
                )
                VALUES ('mtg', ?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING id
                """,
                (
                    self.card_name,
                    owner or self.owner_name,
                    person_id if person_id is not None else self.owner_person_id,
                    quantity,
                    finish,
                    location,
                    deck_id,
                    for_vending,
                ),
            ).fetchone()
            conn.commit()
            return int(row["id"])
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def post_update(
        self,
        item_id: int,
        field: str,
        value,
        *,
        client=None,
        csrf=True,
    ):
        kwargs = {
            "json": {"id": item_id, "field": field, "value": value},
            "base_url": "http://localhost",
        }
        if csrf:
            kwargs["headers"] = {"X-CSRFToken": self.csrf}

        return (client or self.admin_client).post("/update", **kwargs)

    def fetch_inventory(self, item_id: int):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            return conn.execute(
                """
                SELECT id, game_id, name, owner, person_id, quantity,
                       finish, location, deck_id, for_vending
                FROM app.inventory
                WHERE id = ?
                """,
                (item_id,),
            ).fetchone()
        finally:
            conn.close()

    def test_quantity_finish_and_location_updates(self):
        item_id = self.add_inventory()

        response = self.post_update(item_id, "quantity", "7")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(self.fetch_inventory(item_id)["quantity"], 7)

        response = self.post_update(item_id, "finish", "etched")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(self.fetch_inventory(item_id)["finish"], "foil")

        response = self.post_update(item_id, "location", "Deck Alpha")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        row = self.fetch_inventory(item_id)
        self.assertEqual(row["location"], "Deck Alpha")
        self.assertIsNone(row["deck_id"])

    def test_owner_change_updates_owner_and_person_link(self):
        item_id = self.add_inventory()

        response = self.post_update(item_id, "owner", self.target_name)

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        row = self.fetch_inventory(item_id)
        self.assertEqual(row["owner"], self.target_name)
        self.assertEqual(row["person_id"], self.target_person_id)

    def test_inactive_owner_is_rejected_without_mutation(self):
        item_id = self.add_inventory()

        response = self.post_update(item_id, "owner", self.inactive_name)

        self.assertEqual(response.status_code, 400)
        row = self.fetch_inventory(item_id)
        self.assertEqual(row["owner"], self.owner_name)
        self.assertEqual(row["person_id"], self.owner_person_id)

    def test_assigning_deck_clears_vending_flag(self):
        deck_id = self.add_deck()
        item_id = self.add_inventory(for_vending=True)

        response = self.post_update(item_id, "deck_id", deck_id)

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        row = self.fetch_inventory(item_id)
        self.assertEqual(row["deck_id"], deck_id)
        self.assertIs(row["for_vending"], False)

    def test_unassigning_deck_clears_only_deck_membership(self):
        deck_id = self.add_deck()
        item_id = self.add_inventory(deck_id=deck_id)

        response = self.post_update(item_id, "deck_id", "unassigned")

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        row = self.fetch_inventory(item_id)
        self.assertIsNone(row["deck_id"])
        self.assertIs(row["for_vending"], False)

    def test_deck_assigned_card_cannot_be_marked_for_vending(self):
        deck_id = self.add_deck()
        item_id = self.add_inventory(deck_id=deck_id)

        response = self.post_update(item_id, "for_vending", True)

        self.assertEqual(response.status_code, 400)
        self.assertIn("cannot be marked for vending", response.get_json()["error"])
        self.assertIs(self.fetch_inventory(item_id)["for_vending"], False)

    def test_unassigned_card_can_be_marked_for_vending(self):
        item_id = self.add_inventory()

        response = self.post_update(item_id, "for_vending", "yes")

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertIs(self.fetch_inventory(item_id)["for_vending"], True)

    def test_invalid_vending_value_is_rejected(self):
        item_id = self.add_inventory()

        response = self.post_update(item_id, "for_vending", "maybe")

        self.assertEqual(response.status_code, 400)
        self.assertIs(self.fetch_inventory(item_id)["for_vending"], False)

    def test_missing_deck_is_rejected_without_mutation(self):
        item_id = self.add_inventory()

        response = self.post_update(item_id, "deck_id", 999999999)

        self.assertEqual(response.status_code, 404)
        self.assertIsNone(self.fetch_inventory(item_id)["deck_id"])

    def test_missing_inventory_row_returns_404(self):
        response = self.post_update(999999999, "location", "Missing")

        self.assertEqual(response.status_code, 404)

    def test_invalid_id_and_field_are_rejected(self):
        invalid_id = self.admin_client.post(
            "/update",
            json={"id": "not-an-id", "field": "location", "value": "Binder"},
            headers={"X-CSRFToken": self.csrf},
            base_url="http://localhost",
        )
        self.assertEqual(invalid_id.status_code, 400)

        item_id = self.add_inventory()
        invalid_field = self.post_update(item_id, "owner_sql", self.target_name)
        self.assertEqual(invalid_field.status_code, 400)

    def test_update_requires_csrf(self):
        item_id = self.add_inventory()

        response = self.post_update(item_id, "location", "No CSRF", csrf=False)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.fetch_inventory(item_id)["location"], "Binder 1")

    def test_vendor_cannot_update_inventory(self):
        item_id = self.add_inventory()

        response = self.post_update(
            item_id,
            "location",
            "Vendor Attempt",
            client=self.vendor_client,
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.fetch_inventory(item_id)["location"], "Binder 1")


if __name__ == "__main__":
    unittest.main()
