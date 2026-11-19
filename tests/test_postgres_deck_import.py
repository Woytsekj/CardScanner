"""PostgreSQL integration tests for Archidekt/Moxfield/text deck import routes."""

from __future__ import annotations

import io
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from uuid import uuid4

from psycopg.types.json import Jsonb

from postgres_app_db import connect_app_database
from postgres_db import close_pool, open_pool, run_migrations


@unittest.skipUnless(
    os.environ.get("CARD_SCANNER_POSTGRES_DSN")
    and os.environ.get("CARD_SCANNER_DATABASE_BACKEND") == "postgres",
    "Run with the PostgreSQL backend and development DSN configured",
)
class PostgreSQLDeckImportHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        open_pool()
        run_migrations()

        import app as app_module

        if app_module.DATABASE_BACKEND != "postgres":
            raise RuntimeError("Deck import tests require PostgreSQL app mode")
        cls.app_module = app_module
        cls.app_module.app.config["TESTING"] = True

    @classmethod
    def tearDownClass(cls):
        close_pool()

    def setUp(self):
        self.token = uuid4().hex
        self.prefix = f"PGDeckImport{self.token}"
        self.owner = f"PG Deck Import Owner {self.token}"
        self.username = f"pg-deck-import-admin-{self.token}"
        self.csrf = f"csrf-deck-import-{self.token}"
        self.scryfall_id = str(uuid4())
        self.set_code = f"p{self.token[:7]}".lower()
        self.person_id = None
        self.card_id = None
        self.set_id = None
        self.printing_id = None

        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            person = conn.execute(
                """
                INSERT INTO app.people (name, active)
                VALUES (?, TRUE)
                RETURNING id
                """,
                (self.owner,),
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
                (
                    self.username,
                    "unused-pg-deck-import-password-hash",
                    self.person_id,
                ),
            )

            card = conn.execute(
                """
                INSERT INTO catalog.cards (game_id, name)
                VALUES ('mtg', ?)
                RETURNING card_id
                """,
                (f"{self.prefix} Catalog Card",),
            ).fetchone()
            self.card_id = int(card["card_id"])

            set_row = conn.execute(
                """
                INSERT INTO catalog.sets (game_id, name, set_code)
                VALUES ('mtg', ?, ?)
                RETURNING set_id
                """,
                (f"{self.prefix} Test Set", self.set_code),
            ).fetchone()
            self.set_id = int(set_row["set_id"])

            printing = conn.execute(
                """
                INSERT INTO catalog.printings (
                    game_id, card_id, set_id, collector_number, rarity,
                    available_finishes, metadata
                )
                VALUES (
                    'mtg', ?, ?, '17', 'rare',
                    ARRAY['nonfoil', 'foil'],
                    '{"name":"Catalog Card","tcgplayer_id":"123456"}'::jsonb
                )
                RETURNING printing_id
                """,
                (self.card_id, self.set_id),
            ).fetchone()
            self.printing_id = int(printing["printing_id"])

            conn.execute(
                """
                INSERT INTO catalog.external_ids (
                    game_id, provider_id, entity_type, external_id_type,
                    external_id, printing_id
                )
                VALUES (
                    'mtg', 'scryfall', 'printing', 'scryfall_id', ?, ?
                )
                """,
                (self.scryfall_id, self.printing_id),
            )
            conn.execute(
                """
                INSERT INTO catalog.printing_product_mappings (
                    game_id, source_provider_id, source_external_printing_id,
                    target_provider_id, printing_id, target_external_product_id,
                    finishes, card_name, set_code, collector_number, resolved_at
                )
                VALUES (
                    'mtg', 'scryfall', ?, 'tcgcsv', ?, '123456',
                    '["nonfoil","foil"]'::jsonb, ?, ?, '17', now()
                )
                """,
                (self.scryfall_id, self.printing_id, f"{self.prefix} Catalog Card", self.set_code),
            )
            conn.execute(
                """
                UPDATE catalog.game_catalog_state
                SET revision = revision + 1, updated_at = now()
                WHERE game_id = 'mtg'
                """
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        self.client = self.app_module.app.test_client()
        with self.client.session_transaction() as session:
            session["username"] = self.username
            session["csrf_token"] = self.csrf

    def tearDown(self):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            conn.execute(
                "DELETE FROM app.inventory WHERE name LIKE ?",
                (self.prefix + "%",),
            )
            conn.execute(
                "DELETE FROM app.decklists WHERE name LIKE ?",
                (self.prefix + "%",),
            )
            conn.execute(
                "DELETE FROM catalog.printing_product_mappings WHERE source_external_printing_id = ?",
                (self.scryfall_id,),
            )
            conn.execute(
                "DELETE FROM catalog.external_ids WHERE external_id = ?",
                (self.scryfall_id,),
            )
            if self.printing_id is not None:
                conn.execute(
                    "DELETE FROM catalog.printings WHERE printing_id = ? AND game_id = 'mtg'",
                    (self.printing_id,),
                )
            if self.set_id is not None:
                conn.execute(
                    "DELETE FROM catalog.sets WHERE set_id = ? AND game_id = 'mtg'",
                    (self.set_id,),
                )
            if self.card_id is not None:
                conn.execute(
                    "DELETE FROM catalog.cards WHERE card_id = ? AND game_id = 'mtg'",
                    (self.card_id,),
                )
            conn.execute(
                "DELETE FROM app.users WHERE username = ?",
                (self.username,),
            )
            if self.person_id is not None:
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

    def test_text_import_creates_tagged_deck_for_linked_person(self):
        response = self.client.post(
            "/decks/import",
            data={
                "csrf_token": self.csrf,
                "deck_name": f"{self.prefix} Text Deck",
                "deck_text": f"1 Sol Ring\n1 Arcane Signet\n1 {self.prefix} Catalog Card\n\n#Ramp\n1 Cultivate",
                "deck_tags": "Casual, Upgrading",
                "deck_person": self.owner,
            },
            base_url="http://localhost",
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/decks/", response.headers["Location"])

        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            deck = conn.execute(
                """
                SELECT id, source, tags_json, person_id, person_name
                FROM app.decklists
                WHERE name = ?
                """,
                (f"{self.prefix} Text Deck",),
            ).fetchone()
            self.assertIsNotNone(deck)
            self.assertEqual(deck["source"], "text")
            self.assertEqual(int(deck["person_id"]), self.person_id)
            self.assertEqual(deck["person_name"], self.owner)
            self.assertEqual(deck["tags_json"], ["Casual", "Upgrading"])

            cards = conn.execute(
                """
                SELECT name, quantity, category, role_tags_json
                FROM app.deck_cards
                WHERE deck_id = ?
                ORDER BY name
                """,
                (deck["id"],),
            ).fetchall()
            self.assertEqual(len(cards), 4)
            catalog_card = next(
                row for row in cards if row["name"] == f"{self.prefix} Catalog Card"
            )
            self.assertEqual(int(catalog_card["card_id"]), self.card_id)
            self.assertEqual(int(catalog_card["printing_id"]), self.printing_id)
            cultivate = next(row for row in cards if row["name"] == "Cultivate")
            self.assertEqual(cultivate["category"], "Mainboard")
            self.assertEqual(cultivate["role_tags_json"], ["Ramp"])
        finally:
            conn.close()

    def test_csv_import_resolves_printing_and_merges_free_inventory(self):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            conn.execute(
                """
                INSERT INTO app.inventory (
                    game_id, card_id, printing_id, scryfall_id, name, set_code,
                    rarity, price, scan_time, quantity, owner, person_id, finish,
                    collector_number, tcgplayer_id, location, for_vending
                )
                VALUES (
                    'mtg', ?, ?, ?, ?, ?, 'Rare', '1.00', now(), 2,
                    ?, ?, 'normal', '17', '123456', 'Deck Binder', FALSE
                )
                """,
                (
                    self.card_id,
                    self.printing_id,
                    self.scryfall_id,
                    f"{self.prefix} Catalog Card",
                    self.set_code,
                    self.owner,
                    self.person_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        csv_data = (
            "Name,Set code,Collector number,Scryfall ID,Foil,Quantity,Tags\n"
            f"{self.prefix} Catalog Card,{self.set_code},17,{self.scryfall_id},false,3,Draw\n"
        ).encode("utf-8")
        with patch.object(
            self.app_module,
            "get_tcg_prices",
            return_value={"123456": {"normal": 2.75, "foil": 4.50}},
        ):
            response = self.client.post(
                "/decks/import",
                data={
                    "csrf_token": self.csrf,
                    "deck_name": f"{self.prefix} CSV Deck",
                    "deck_file": (io.BytesIO(csv_data), "deck.csv"),
                    "deck_tags": "Commander Night",
                    "deck_person": self.owner,
                    "owner": self.owner,
                    "add_inventory": "on",
                    "store_free": "on",
                    "location": "Deck Binder",
                },
                content_type="multipart/form-data",
                base_url="http://localhost",
            )

        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        self.assertIn("/decks/", response.headers["Location"])

        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            deck = conn.execute(
                """
                SELECT id, source, tags_json, person_id
                FROM app.decklists
                WHERE name = ?
                """,
                (f"{self.prefix} CSV Deck",),
            ).fetchone()
            self.assertIsNotNone(deck)
            self.assertEqual(deck["source"], "deck-csv")
            self.assertEqual(deck["tags_json"], ["Commander Night"])
            self.assertEqual(int(deck["person_id"]), self.person_id)

            requirement = conn.execute(
                """
                SELECT name, quantity, scryfall_id, card_id, printing_id,
                       set_code, collector_number, tcgplayer_id, finish,
                       role_tags_json
                FROM app.deck_cards
                WHERE deck_id = ?
                """,
                (deck["id"],),
            ).fetchone()
            self.assertEqual(requirement["quantity"], 3)
            self.assertEqual(requirement["scryfall_id"], self.scryfall_id)
            self.assertEqual(int(requirement["card_id"]), self.card_id)
            self.assertEqual(int(requirement["printing_id"]), self.printing_id)
            self.assertEqual(requirement["tcgplayer_id"], "123456")
            self.assertEqual(requirement["finish"], "normal")
            self.assertEqual(requirement["role_tags_json"], ["Draw"])

            inventory = conn.execute(
                """
                SELECT quantity, price, deck_id, for_vending, person_id,
                       location, tcgplayer_id, rarity
                FROM app.inventory
                WHERE scryfall_id = ?
                """,
                (self.scryfall_id,),
            ).fetchone()
            self.assertEqual(inventory["quantity"], 5)
            self.assertEqual(inventory["price"], "2.75")
            self.assertIsNone(inventory["deck_id"])
            self.assertFalse(inventory["for_vending"])
            self.assertEqual(int(inventory["person_id"]), self.person_id)
            self.assertEqual(inventory["location"], "Deck Binder")
            self.assertEqual(inventory["tcgplayer_id"], "123456")
            self.assertEqual(inventory["rarity"], "Rare")
        finally:
            conn.close()

    def test_inactive_inventory_owner_does_not_create_partial_deck(self):
        response = self.client.post(
            "/decks/import",
            data={
                "csrf_token": self.csrf,
                "deck_name": f"{self.prefix} Invalid Owner Deck",
                "deck_text": "1 Sol Ring",
                "deck_person": self.owner,
                "owner": "No Such Active Person",
                "add_inventory": "on",
            },
            base_url="http://localhost",
        )
        self.assertEqual(response.status_code, 302)

        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            count = conn.execute(
                "SELECT COUNT(*) AS count FROM app.decklists WHERE name = ?",
                (f"{self.prefix} Invalid Owner Deck",),
            ).fetchone()["count"]
            self.assertEqual(int(count), 0)
        finally:
            conn.close()

    def test_import_requires_admin_and_csrf(self):
        guest = self.app_module.app.test_client()
        response = guest.post(
            "/decks/import",
            data={"deck_text": "1 Sol Ring"},
            base_url="http://localhost",
        )
        self.assertIn(response.status_code, {302, 401})

        no_csrf = self.app_module.app.test_client()
        with no_csrf.session_transaction() as session:
            session["username"] = self.username
        response = no_csrf.post(
            "/decks/import",
            data={
                "deck_name": f"{self.prefix} Missing CSRF",
                "deck_text": "1 Sol Ring",
                "deck_person": self.owner,
            },
            base_url="http://localhost",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("CSRF", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
