"""PostgreSQL integration tests for inventory quantity movement."""

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
class PostgreSQLInventoryMoveTests(unittest.TestCase):
    RESOLVED_SCRYFALL_ID = "0000579f-7b35-4ed3-b44c-db2a538066fe"

    @classmethod
    def setUpClass(cls):
        open_pool()
        run_migrations()

        import app as app_module

        if app_module.DATABASE_BACKEND != "postgres":
            raise RuntimeError("Inventory movement tests require PostgreSQL app mode")

        cls.app_module = app_module
        cls.app_module.app.config["TESTING"] = True

    @classmethod
    def tearDownClass(cls):
        close_pool()

    def setUp(self):
        self.token = uuid4().hex
        self.owner_name = f"PG Inventory Move Owner {self.token}"
        self.card_name = f"Inventory Move Card {self.token}"
        self.admin_username = f"pg-inv-move-admin-{self.token}"
        self.vendor_username = f"pg-inv-move-vendor-{self.token}"
        self.csrf = f"csrf-inv-move-{self.token}"
        self.deck_ids = []

        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            catalog = conn.execute(
                """
                SELECT p.card_id, e.printing_id
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
                (self.RESOLVED_SCRYFALL_ID,),
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
                    is_superadmin, active
                )
                VALUES (?, ?, TRUE, 'admin', FALSE, TRUE)
                """,
                (self.admin_username, "unused-inventory-move-admin-hash"),
            )
            conn.execute(
                """
                INSERT INTO app.users (
                    username, password_hash, is_admin, role,
                    is_superadmin, active
                )
                VALUES (?, ?, FALSE, 'vendor', FALSE, TRUE)
                """,
                (self.vendor_username, "unused-inventory-move-vendor-hash"),
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

            for deck_id in self.deck_ids:
                conn.execute(
                    "DELETE FROM app.decklists WHERE id = ?",
                    (deck_id,),
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

    def add_deck(self) -> int:
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
                (f"PG Inventory Move Deck {self.token}",),
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
        name=None,
        set_code="TST",
        scryfall_id: str | None = RESOLVED_SCRYFALL_ID,
        finish="normal",
        quantity=3,
        location="Binder 1",
        deck_id=None,
        for_vending=False,
        price="1.25",
        tcgplayer_id="123456",
    ) -> int:
        if game_id == "mtg":
            card_id = self.card_id
            printing_id = self.printing_id
        else:
            card_id = None
            printing_id = None

        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            row = conn.execute(
                """
                INSERT INTO app.inventory (
                    game_id, card_id, printing_id, scryfall_id,
                    name, set_code, rarity, price, quantity,
                    owner, person_id, finish, collector_number,
                    tcgplayer_id, location, deck_id, for_vending
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, 'Rare', ?, ?, ?, ?,
                    ?, '001', ?, ?, ?, ?
                )
                RETURNING id
                """,
                (
                    game_id,
                    card_id,
                    printing_id,
                    scryfall_id,
                    name or self.card_name,
                    set_code,
                    price,
                    quantity,
                    self.owner_name,
                    self.person_id,
                    finish,
                    tcgplayer_id,
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

    def rows_for_owner(self):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            return conn.execute(
                """
                SELECT
                    id, game_id, card_id, printing_id, scryfall_id,
                    name, set_code, rarity, price, scan_time, quantity,
                    owner, person_id, finish, collector_number,
                    tcgplayer_id, location, deck_id, for_vending
                FROM app.inventory
                WHERE owner = ?
                ORDER BY id
                """,
                (self.owner_name,),
            ).fetchall()
        finally:
            conn.close()

    def fetch_inventory(self, item_id: int):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            return conn.execute(
                """
                SELECT *
                FROM app.inventory
                WHERE id = ?
                """,
                (item_id,),
            ).fetchone()
        finally:
            conn.close()

    def post_move(self, item_id, quantity, destination, *, client=None, csrf=True):
        kwargs = {
            "json": {
                "id": item_id,
                "quantity": quantity,
                "destination": destination,
            },
            "base_url": "http://localhost",
        }
        if csrf:
            kwargs["headers"] = {"X-CSRFToken": self.csrf}
        return (client or self.admin_client).post("/admin/move_quantity", **kwargs)

    def test_partial_move_creates_row_and_preserves_metadata(self):
        item_id = self.add_inventory(
            finish="foil",
            quantity=3,
            location="Binder 1",
            for_vending=True,
            price="7.25",
            tcgplayer_id="987654",
        )
        original = self.fetch_inventory(item_id)

        response = self.post_move(item_id, 2, "Binder 2")

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(
            response.get_json(),
            {"success": True, "message": "Moved 2 card(s) to Binder 2."},
        )

        rows = self.rows_for_owner()
        self.assertEqual(len(rows), 2)

        source = next(row for row in rows if row["id"] == item_id)
        destination = next(row for row in rows if row["id"] != item_id)

        self.assertEqual(source["quantity"], 1)
        self.assertEqual(source["location"], "Binder 1")
        self.assertEqual(destination["quantity"], 2)
        self.assertEqual(destination["location"], "Binder 2")

        for field in (
            "game_id", "card_id", "printing_id", "scryfall_id",
            "name", "set_code", "rarity", "price", "owner",
            "person_id", "finish", "collector_number",
            "tcgplayer_id", "deck_id", "for_vending",
        ):
            self.assertEqual(destination[field], original[field], field)

        self.assertEqual(destination["scan_time"], original["scan_time"])

    def test_partial_move_merges_compatible_scryfall_row(self):
        source_id = self.add_inventory(quantity=3, location="Binder 1")
        target_id = self.add_inventory(quantity=4, location="Binder 2")

        response = self.post_move(source_id, 2, "Binder 2")

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        rows = self.rows_for_owner()
        self.assertEqual(len(rows), 2)

        source = self.fetch_inventory(source_id)
        target = self.fetch_inventory(target_id)
        self.assertEqual(source["quantity"], 1)
        self.assertEqual(target["quantity"], 6)
        self.assertEqual(target["location"], "Binder 2")

    def test_partial_move_merges_case_insensitive_name_and_set_match(self):
        source_id = self.add_inventory(
            scryfall_id=None,
            name="Name Match Card",
            set_code="ABC",
            quantity=3,
            location="Binder 1",
        )
        target_id = self.add_inventory(
            scryfall_id=None,
            name="name match card",
            set_code="abc",
            quantity=4,
            location="Binder 2",
        )

        self.assertIsNone(self.fetch_inventory(source_id)["scryfall_id"])
        self.assertIsNone(self.fetch_inventory(target_id)["scryfall_id"])
        response = self.post_move(source_id, 1, "Binder 2")

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(self.fetch_inventory(source_id)["quantity"], 2)
        self.assertEqual(self.fetch_inventory(target_id)["quantity"], 5)
        self.assertEqual(len(self.rows_for_owner()), 2)

    def test_incompatible_finish_does_not_merge(self):
        source_id = self.add_inventory(finish="normal", quantity=3)
        foil_target_id = self.add_inventory(
            finish="foil",
            quantity=4,
            location="Binder 2",
        )

        response = self.post_move(source_id, 1, "Binder 2")

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        rows = self.rows_for_owner()
        self.assertEqual(len(rows), 3)
        self.assertEqual(self.fetch_inventory(source_id)["quantity"], 2)
        self.assertEqual(self.fetch_inventory(foil_target_id)["quantity"], 4)

        new_row = next(
            row for row in rows
            if row["id"] not in {source_id, foil_target_id}
        )
        self.assertEqual(new_row["quantity"], 1)
        self.assertEqual(new_row["finish"], "normal")
        self.assertEqual(new_row["location"], "Binder 2")

    def test_full_move_changes_source_location_without_merging(self):
        source_id = self.add_inventory(quantity=1, location="Binder 1")
        target_id = self.add_inventory(quantity=4, location="Binder 2")

        response = self.post_move(source_id, 1, "Binder 2")

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(self.fetch_inventory(source_id)["location"], "Binder 2")
        self.assertEqual(self.fetch_inventory(source_id)["quantity"], 1)
        self.assertEqual(self.fetch_inventory(target_id)["quantity"], 4)
        self.assertEqual(len(self.rows_for_owner()), 2)

    def test_split_preserves_deck_assignment(self):
        deck_id = self.add_deck()
        source_id = self.add_inventory(
            quantity=3,
            deck_id=deck_id,
            for_vending=False,
        )

        response = self.post_move(source_id, 1, "Binder 2")

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        rows = self.rows_for_owner()
        destination = next(row for row in rows if row["id"] != source_id)

        self.assertEqual(destination["quantity"], 1)
        self.assertEqual(destination["deck_id"], deck_id)
        self.assertIs(destination["for_vending"], False)
        self.assertEqual(destination["person_id"], self.person_id)

    def test_quantity_above_available_is_rejected_without_mutation(self):
        item_id = self.add_inventory(quantity=2)

        response = self.post_move(item_id, 3, "Binder 2")

        self.assertEqual(response.status_code, 400)
        self.assertIn("only have 2", response.get_json()["error"])
        self.assertEqual(self.fetch_inventory(item_id)["quantity"], 2)
        self.assertEqual(len(self.rows_for_owner()), 1)

    def test_missing_inventory_row_returns_404(self):
        response = self.post_move(999999999, 1, "Binder 2")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["error"], "Inventory row not found.")

    def test_same_location_is_a_noop_case_insensitively(self):
        item_id = self.add_inventory(quantity=3, location="Binder 1")

        response = self.post_move(item_id, 1, "binder 1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()["message"],
            "Destination is the current location.",
        )
        self.assertEqual(self.fetch_inventory(item_id)["quantity"], 3)
        self.assertEqual(self.fetch_inventory(item_id)["location"], "Binder 1")

    def test_blank_destination_defaults_to_free(self):
        item_id = self.add_inventory(quantity=1, location="Binder 1")

        response = self.post_move(item_id, 1, "   ")

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(self.fetch_inventory(item_id)["location"], "Free")

    def test_move_requires_csrf(self):
        item_id = self.add_inventory()

        response = self.post_move(item_id, 1, "Binder 2", csrf=False)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.fetch_inventory(item_id)["quantity"], 3)
        self.assertEqual(len(self.rows_for_owner()), 1)

    def test_vendor_cannot_move_inventory(self):
        item_id = self.add_inventory()

        response = self.post_move(
            item_id,
            1,
            "Binder 2",
            client=self.vendor_client,
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.fetch_inventory(item_id)["quantity"], 3)
        self.assertEqual(len(self.rows_for_owner()), 1)

    def test_move_does_not_affect_inventory_from_another_game(self):
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

        item_id = self.add_inventory(
            game_id=other_game["game_id"],
            quantity=2,
            location="Binder 1",
        )

        response = self.post_move(item_id, 1, "Binder 2")

        self.assertEqual(response.status_code, 404)
        remaining = self.fetch_inventory(item_id)
        self.assertIsNotNone(remaining)
        self.assertEqual(remaining["game_id"], other_game["game_id"])
        self.assertEqual(remaining["quantity"], 2)
        self.assertEqual(remaining["location"], "Binder 1")
