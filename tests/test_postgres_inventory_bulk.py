"""PostgreSQL integration tests for bulk MTG inventory updates."""

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
class PostgreSQLInventoryBulkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        open_pool()
        run_migrations()

        import app as app_module

        if app_module.DATABASE_BACKEND != "postgres":
            raise RuntimeError("Bulk inventory tests require PostgreSQL app mode")

        cls.app_module = app_module
        cls.app_module.app.config["TESTING"] = True

    @classmethod
    def tearDownClass(cls):
        close_pool()

    def setUp(self):
        self.token = uuid4().hex
        self.owner_name = f"PG Inventory Bulk Owner {self.token}"
        self.target_name = f"PG Inventory Bulk Target {self.token}"
        self.inactive_name = f"PG Inventory Bulk Inactive {self.token}"
        self.admin_username = f"pg-invbulk-admin-{self.token}"
        self.vendor_username = f"pg-invbulk-vendor-{self.token}"
        self.card_name = f"Inventory Bulk Card {self.token}"
        self.csrf = f"csrf-invbulk-{self.token}"
        self.inventory_ids: list[int] = []
        self.deck_ids: list[int] = []

        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            person_ids = []

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
                person_ids.append(int(row["id"]))

            (
                self.owner_person_id,
                self.target_person_id,
                self.inactive_person_id,
            ) = person_ids

            conn.execute(
                """
                INSERT INTO app.users (
                    username, password_hash, is_admin, role,
                    is_superadmin, active, person_id
                )
                VALUES (?, ?, TRUE, 'admin', FALSE, TRUE, ?)
                """,
                (self.admin_username, "unused-inventory-bulk-admin-hash", self.owner_person_id),
            )
            conn.execute(
                """
                INSERT INTO app.users (
                    username, password_hash, is_admin, role,
                    is_superadmin, active
                )
                VALUES (?, ?, FALSE, 'vendor', FALSE, TRUE)
                """,
                (self.vendor_username, "unused-inventory-bulk-vendor-hash"),
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
                    f"pg-invbulk-user-{self.token}",
                    "unused-inventory-bulk-user-hash",
                    self.target_person_id,
                    self.admin_username,
                ),
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
            if self.inventory_ids:
                placeholders = ", ".join("?" for _ in self.inventory_ids)
                conn.execute(
                    f"DELETE FROM app.inventory WHERE id IN ({placeholders})",
                    self.inventory_ids,
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
                    f"pg-invbulk-user-{self.token}",
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

    def add_deck(self, *, game_id="mtg") -> int:
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            row = conn.execute(
                """
                INSERT INTO app.decklists (
                    game_id, name, source, created_at, updated_at, tags_json,
                    person_id, person_name
                )
                VALUES (?, ?, 'manual', now(), now(), '[]'::jsonb, ?, ?)
                RETURNING id
                """,
                (
                    game_id,
                    f"PG Inventory Bulk Deck {self.token} {len(self.deck_ids)}",
                    self.owner_person_id,
                    self.owner_name,
                ),
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
        game_id="mtg",
        deck_id=None,
        for_vending=False,
        location="Binder 1",
        owner=None,
        person_id=None,
    ) -> int:
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            row = conn.execute(
                """
                INSERT INTO app.inventory (
                    game_id, name, owner, person_id, quantity,
                    finish, location, deck_id, for_vending
                )
                VALUES (?, ?, ?, ?, 2, 'normal', ?, ?, ?)
                RETURNING id
                """,
                (
                    game_id,
                    self.card_name,
                    owner or self.owner_name,
                    person_id if person_id is not None else self.owner_person_id,
                    location,
                    deck_id,
                    for_vending,
                ),
            ).fetchone()
            conn.commit()
            item_id = int(row["id"])
            self.inventory_ids.append(item_id)
            return item_id
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
                SELECT id, game_id, owner, person_id, quantity,
                       location, deck_id, for_vending
                FROM app.inventory
                WHERE id = ?
                """,
                (item_id,),
            ).fetchone()
        finally:
            conn.close()

    def post_bulk(self, payload, *, client=None, csrf=True):
        kwargs = {
            "json": payload,
            "base_url": "http://localhost",
        }
        if csrf:
            kwargs["headers"] = {"X-CSRFToken": self.csrf}

        return (client or self.admin_client).post(
            "/admin/inventory/bulk-update",
            **kwargs,
        )

    def test_owner_change_updates_owner_and_person_link(self):
        first = self.add_inventory(location="Binder 1", for_vending=True)
        second = self.add_inventory(location="Storage Box", for_vending=False)

        response = self.post_bulk({
            "ids": [first, second],
            "owner": self.target_name,
        })

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json()["updated"], 2)
        self.assertEqual(response.get_json()["selected"], 2)

        for item_id, location, vending in (
            (first, "Binder 1", True),
            (second, "Storage Box", False),
        ):
            row = self.fetch_inventory(item_id)
            self.assertEqual(row["owner"], self.target_name)
            self.assertEqual(row["person_id"], self.target_person_id)
            self.assertEqual(row["location"], location)
            self.assertIs(row["for_vending"], vending)

    def test_inactive_owner_rejected_without_any_partial_mutation(self):
        first = self.add_inventory(location="Binder 1")
        second = self.add_inventory(location="Binder 2")

        response = self.post_bulk({
            "ids": [first, second],
            "owner": self.inactive_name,
            "location": "Changed Shelf",
        })

        self.assertEqual(response.status_code, 400)
        for item_id, original_location in (
            (first, "Binder 1"),
            (second, "Binder 2"),
        ):
            row = self.fetch_inventory(item_id)
            self.assertEqual(row["owner"], self.owner_name)
            self.assertEqual(row["person_id"], self.owner_person_id)
            self.assertEqual(row["location"], original_location)

    def test_assigning_deck_clears_vending_flag(self):
        deck_id = self.add_deck()
        item_id = self.add_inventory(for_vending=True)

        response = self.post_bulk({
            "ids": [item_id],
            "deck_id": str(deck_id),
        })

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        row = self.fetch_inventory(item_id)
        self.assertEqual(row["deck_id"], deck_id)
        self.assertIs(row["for_vending"], False)

    def test_unassigning_and_marking_for_vending_can_be_combined(self):
        deck_id = self.add_deck()
        item_id = self.add_inventory(deck_id=deck_id, location="Binder 3")

        response = self.post_bulk({
            "ids": [item_id],
            "deck_id": "none",
            "for_vending": "1",
        })

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        row = self.fetch_inventory(item_id)
        self.assertIsNone(row["deck_id"])
        self.assertIs(row["for_vending"], True)
        self.assertEqual(row["location"], "Binder 3")

    def test_assigning_deck_and_marking_for_vending_together_is_rejected(self):
        deck_id = self.add_deck()
        item_id = self.add_inventory(location="Binder 4")

        response = self.post_bulk({
            "ids": [item_id],
            "deck_id": deck_id,
            "for_vending": "1",
        })

        self.assertEqual(response.status_code, 400)
        row = self.fetch_inventory(item_id)
        self.assertIsNone(row["deck_id"])
        self.assertIs(row["for_vending"], False)

    def test_existing_deck_assignment_blocks_vending_for_entire_batch(self):
        deck_id = self.add_deck()
        assigned = self.add_inventory(deck_id=deck_id)
        unassigned = self.add_inventory(location="Binder 2")

        response = self.post_bulk({
            "ids": [assigned, unassigned],
            "for_vending": "1",
        })

        self.assertEqual(response.status_code, 400)
        self.assertIn("1 selected row(s) are assigned to decks", response.get_json()["error"])
        self.assertIs(self.fetch_inventory(assigned)["for_vending"], False)
        self.assertIs(self.fetch_inventory(unassigned)["for_vending"], False)

    def test_missing_or_non_mtg_deck_is_rejected_without_mutation(self):
        item_id = self.add_inventory()

        response = self.post_bulk({
            "ids": [item_id],
            "deck_id": 999999999,
            "location": "Should Not Change",
        })

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.fetch_inventory(item_id)["location"], "Binder 1")
        self.assertIsNone(self.fetch_inventory(item_id)["deck_id"])

    def test_invalid_vending_value_is_rejected(self):
        item_id = self.add_inventory()

        response = self.post_bulk({
            "ids": [item_id],
            "for_vending": "maybe",
        })

        self.assertEqual(response.status_code, 400)
        self.assertIs(self.fetch_inventory(item_id)["for_vending"], False)

    def test_location_is_trimmed_limited_and_blank_defaults_to_free(self):
        blank = self.add_inventory(location="Binder 1")
        long_value = self.add_inventory(location="Binder 2")

        response = self.post_bulk({
            "ids": [blank],
            "location": "   ",
        })
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(self.fetch_inventory(blank)["location"], "Free")

        long_location = "X" * 130
        response = self.post_bulk({
            "ids": [long_value],
            "location": f"  {long_location}  ",
        })
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(self.fetch_inventory(long_value)["location"], "X" * 120)

    def test_ids_are_deduplicated_and_invalid_values_ignored(self):
        item_id = self.add_inventory()

        response = self.post_bulk({
            "ids": [item_id, str(item_id), "not-an-id", None, 0, -1],
            "location": "Shelf 9",
        })

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json()["selected"], 1)
        self.assertEqual(response.get_json()["updated"], 1)
        self.assertEqual(self.fetch_inventory(item_id)["location"], "Shelf 9")

    def test_non_mtg_rows_are_not_updated_in_a_mixed_selection(self):
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

        mtg_id = self.add_inventory(location="Binder 1")
        other_id = self.add_inventory(
            game_id=other_game["game_id"],
            location="Other Game Storage",
        )

        response = self.post_bulk({
            "ids": [mtg_id, other_id],
            "location": "Bulk Changed",
        })

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json()["selected"], 2)
        self.assertEqual(response.get_json()["updated"], 1)
        self.assertEqual(self.fetch_inventory(mtg_id)["location"], "Bulk Changed")

        other_row = self.fetch_inventory(other_id)
        self.assertEqual(other_row["game_id"], other_game["game_id"])
        self.assertEqual(other_row["location"], "Other Game Storage")

    def test_missing_positive_id_returns_success_with_zero_updated(self):
        response = self.post_bulk({
            "ids": [999999999],
            "location": "Unused Shelf",
        })

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json()["updated"], 0)
        self.assertEqual(response.get_json()["selected"], 1)

    def test_invalid_ids_and_non_list_ids_are_rejected(self):
        for payload in (
            {"ids": "123", "location": "Shelf"},
            {"ids": [0, -1, "invalid"], "location": "Shelf"},
        ):
            response = self.post_bulk(payload)
            self.assertEqual(response.status_code, 400)

    def test_more_than_5000_unique_ids_are_rejected(self):
        response = self.post_bulk({
            "ids": list(range(1, 5002)),
            "location": "Should Not Apply",
        })

        self.assertEqual(response.status_code, 400)
        self.assertIn("5,000", response.get_json()["error"])

    def test_request_without_any_change_is_rejected(self):
        item_id = self.add_inventory()

        response = self.post_bulk({"ids": [item_id]})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.fetch_inventory(item_id)["location"], "Binder 1")

    def test_bulk_update_requires_csrf(self):
        item_id = self.add_inventory()

        response = self.post_bulk(
            {"ids": [item_id], "location": "No CSRF"},
            csrf=False,
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.fetch_inventory(item_id)["location"], "Binder 1")

    def test_vendor_cannot_bulk_update_inventory(self):
        item_id = self.add_inventory()

        response = self.post_bulk(
            {"ids": [item_id], "location": "Vendor Attempt"},
            client=self.vendor_client,
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.fetch_inventory(item_id)["location"], "Binder 1")


if __name__ == "__main__":
    unittest.main()
