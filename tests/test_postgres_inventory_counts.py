"""PostgreSQL integration tests for inventory finish-count updates."""

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
class PostgreSQLInventoryCountsTests(unittest.TestCase):
    RESOLVED_SCRYFALL_ID = "0000579f-7b35-4ed3-b44c-db2a538066fe"

    @classmethod
    def setUpClass(cls):
        open_pool()
        run_migrations()

        import app as app_module

        if app_module.DATABASE_BACKEND != "postgres":
            raise RuntimeError("Inventory-count tests require PostgreSQL app mode")

        cls.app_module = app_module
        cls.app_module.app.config["TESTING"] = True

    @classmethod
    def tearDownClass(cls):
        close_pool()

    def setUp(self):
        self.token = uuid4().hex
        self.admin_username = f"pg-invcounts-admin-{self.token}"
        self.vendor_username = f"pg-invcounts-vendor-{self.token}"
        self.owner_name = f"PG Inventory Counts {self.token}"
        self.csrf = f"csrf-invcounts-{self.token}"
        self.scryfall_id = self.RESOLVED_SCRYFALL_ID
        self.card_name = f"Inventory Count Test Card {self.token}"

        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            catalog = conn.execute(
                """
                SELECT
                    p.card_id,
                    e.printing_id
                FROM catalog.external_ids AS e
                JOIN catalog.printings AS p
                  ON p.printing_id = e.printing_id
                 AND p.game_id = e.game_id
                WHERE e.game_id = 'mtg'
                  AND e.provider_id = 'scryfall'
                  AND e.entity_type = 'printing'
                  AND e.external_id_type = 'scryfall_id'
                  AND e.external_id = ?
                LIMIT 1
                """,
                (self.scryfall_id,),
            ).fetchone()

            if not catalog:
                raise RuntimeError(
                    "Expected resolved Scryfall fixture is missing from cardscanner_dev"
                )

            self.card_id = catalog["card_id"]
            self.printing_id = catalog["printing_id"]

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
                (self.admin_username, "unused-inventory-count-test-hash", self.person_id),
            )
            conn.execute(
                """
                INSERT INTO app.users (
                    username, password_hash, is_admin, role,
                    is_superadmin, active
                )
                VALUES (?, ?, FALSE, 'vendor', FALSE, TRUE)
                """,
                (self.vendor_username, "unused-inventory-count-test-hash"),
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

    def insert_inventory_row(self, finish: str, quantity: int = 1) -> int:
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            row = conn.execute(
                """
                INSERT INTO app.inventory (
                    game_id,
                    card_id,
                    printing_id,
                    scryfall_id,
                    name,
                    set_code,
                    rarity,
                    price,
                    quantity,
                    owner,
                    person_id,
                    finish,
                    collector_number,
                    tcgplayer_id,
                    location,
                    deck_id,
                    for_vending
                )
                VALUES (
                    'mtg', ?, ?, ?, ?, 'TST', 'Rare', '1.25',
                    ?, ?, ?, ?, 'COUNT-01', '123456',
                    'Count Binder', NULL, TRUE
                )
                RETURNING id
                """,
                (
                    self.card_id,
                    self.printing_id,
                    self.scryfall_id,
                    self.card_name,
                    quantity,
                    self.owner_name,
                    self.person_id,
                    finish,
                ),
            ).fetchone()
            conn.commit()
            return int(row["id"])
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def post_counts(self, *, client=None, headers=True, **overrides):
        payload = {
            "scryfall_id": self.scryfall_id,
            "owner": self.owner_name,
            "normal": 0,
            "foil": 1,
        }
        payload.update(overrides)

        kwargs = {"json": payload, "base_url": "http://localhost"}
        if headers:
            kwargs["headers"] = {"X-CSRFToken": self.csrf}

        return (client or self.admin_client).post("/admin/set_counts", **kwargs)

    def fetch_rows(self):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            return conn.execute(
                """
                SELECT
                    id, game_id, card_id, printing_id, scryfall_id, name,
                    set_code, rarity, price, quantity, owner, person_id,
                    finish, collector_number, tcgplayer_id, location,
                    deck_id, for_vending
                FROM app.inventory
                WHERE owner = ? AND scryfall_id = ?
                ORDER BY finish, id
                """,
                (self.owner_name, self.scryfall_id),
            ).fetchall()
        finally:
            conn.close()

    def test_normal_to_foil_conversion_preserves_catalog_and_collection_metadata(self):
        original_id = self.insert_inventory_row("normal", quantity=1)

        response = self.post_counts(normal=0, foil=2)

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertTrue(response.get_json()["success"])

        rows = self.fetch_rows()
        self.assertEqual(len(rows), 1)

        row = rows[0]
        self.assertNotEqual(row["id"], original_id)
        self.assertEqual(row["game_id"], "mtg")
        self.assertEqual(row["card_id"], self.card_id)
        self.assertEqual(row["printing_id"], self.printing_id)
        self.assertEqual(row["scryfall_id"], self.scryfall_id)
        self.assertEqual(row["name"], self.card_name)
        self.assertEqual(row["set_code"], "TST")
        self.assertEqual(row["rarity"], "Rare")
        self.assertEqual(row["price"], "1.25")
        self.assertEqual(row["quantity"], 2)
        self.assertEqual(row["owner"], self.owner_name)
        self.assertEqual(row["person_id"], self.person_id)
        self.assertEqual(row["finish"], "foil")
        self.assertEqual(row["collector_number"], "COUNT-01")
        self.assertEqual(row["tcgplayer_id"], "123456")
        self.assertEqual(row["location"], "Count Binder")
        self.assertIsNone(row["deck_id"])
        self.assertIs(row["for_vending"], True)

    def test_existing_finish_rows_are_updated_and_zero_finish_is_deleted(self):
        normal_id = self.insert_inventory_row("normal", quantity=1)
        self.insert_inventory_row("foil", quantity=5)

        response = self.post_counts(normal=4, foil=0)

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        rows = self.fetch_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], normal_id)
        self.assertEqual(rows[0]["finish"], "normal")
        self.assertEqual(rows[0]["quantity"], 4)

    def test_unknown_card_returns_404_without_inserting_inventory(self):
        response = self.post_counts()

        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.get_json()["success"])
        self.assertEqual(self.fetch_rows(), [])

    def test_invalid_count_returns_400_without_mutating_inventory(self):
        self.insert_inventory_row("normal", quantity=2)

        response = self.post_counts(normal="not-a-number", foil=1)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["success"])
        rows = self.fetch_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["quantity"], 2)
        self.assertEqual(rows[0]["finish"], "normal")

    def test_set_counts_requires_csrf(self):
        self.insert_inventory_row("normal")

        response = self.post_counts(headers=False)

        self.assertEqual(response.status_code, 400)
        self.assertIn("CSRF", response.get_data(as_text=True))
        rows = self.fetch_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["finish"], "normal")

    def test_vendor_cannot_change_inventory_counts(self):
        self.insert_inventory_row("normal")

        response = self.post_counts(client=self.vendor_client)

        self.assertEqual(response.status_code, 403)
        rows = self.fetch_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["finish"], "normal")


if __name__ == "__main__":
    unittest.main()
