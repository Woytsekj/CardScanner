"""PostgreSQL integration tests for MTG deck-card add, edit, and delete."""

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
class PostgreSQLDeckCardTests(unittest.TestCase):
    RESOLVED_SCRYFALL_ID = "0000579f-7b35-4ed3-b44c-db2a538066fe"

    @classmethod
    def setUpClass(cls):
        open_pool()
        run_migrations()

        import app as app_module

        if app_module.DATABASE_BACKEND != "postgres":
            raise RuntimeError("Deck-card tests require PostgreSQL app mode")

        cls.app_module = app_module
        cls.app_module.app.config["TESTING"] = True

    @classmethod
    def tearDownClass(cls):
        close_pool()

    def setUp(self):
        self.token = uuid4().hex
        self.admin_username = f"pg-deckcard-admin-{self.token}"
        self.vendor_username = f"pg-deckcard-vendor-{self.token}"
        self.csrf = f"csrf-deckcard-{self.token}"
        self.deck_ids = []

        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            conn.execute(
                """
                INSERT INTO app.users (
                    username, password_hash, is_admin, role,
                    is_superadmin, active
                )
                VALUES (?, ?, TRUE, 'admin', FALSE, TRUE)
                """,
                (self.admin_username, "unused-deck-card-admin-hash"),
            )
            conn.execute(
                """
                INSERT INTO app.users (
                    username, password_hash, is_admin, role,
                    is_superadmin, active
                )
                VALUES (?, ?, FALSE, 'vendor', FALSE, TRUE)
                """,
                (self.vendor_username, "unused-deck-card-vendor-hash"),
            )

            self.deck_id = self.add_deck_with_connection(
                conn, f"PG Deck Card Test {self.token}"
            )
            self.deck_ids.append(self.deck_id)

            self.other_deck_id = self.add_deck_with_connection(
                conn, f"PG Deck Card Other {self.token}"
            )
            self.deck_ids.append(self.other_deck_id)

            self.catalog = conn.execute(
                """
                SELECT
                    e.external_id AS scryfall_id,
                    COALESCE(c.name, p.metadata->>'name') AS name,
                    s.set_code,
                    p.collector_number,
                    p.card_id,
                    p.printing_id,
                    (
                        SELECT m.target_external_product_id
                        FROM catalog.printing_product_mappings AS m
                        WHERE m.game_id = p.game_id
                          AND m.printing_id = p.printing_id
                          AND m.source_provider_id = 'scryfall'
                          AND m.source_external_printing_id = e.external_id
                          AND m.target_provider_id = 'tcgcsv'
                        ORDER BY m.target_external_product_id
                        LIMIT 1
                    ) AS tcgplayer_id
                FROM catalog.external_ids AS e
                JOIN catalog.printings AS p
                  ON p.printing_id = e.printing_id
                 AND p.game_id = e.game_id
                LEFT JOIN catalog.cards AS c
                  ON c.card_id = p.card_id
                 AND c.game_id = p.game_id
                LEFT JOIN catalog.sets AS s
                  ON s.set_id = p.set_id
                 AND s.game_id = p.game_id
                WHERE e.game_id = 'mtg'
                  AND e.provider_id = 'scryfall'
                  AND e.entity_type = 'printing'
                  AND e.external_id_type = 'scryfall_id'
                  AND e.external_id = ?
                LIMIT 1
                """,
                (self.RESOLVED_SCRYFALL_ID,),
            ).fetchone()

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

    @staticmethod
    def add_deck_with_connection(conn, name: str) -> int:
        row = conn.execute(
            """
            INSERT INTO app.decklists (
                game_id, name, source, created_at, updated_at
            )
            VALUES ('mtg', ?, 'manual', now(), now())
            RETURNING id
            """,
            (name,),
        ).fetchone()
        return int(row["id"])

    def add_deck(self, name: str | None = None) -> int:
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            deck_id = self.add_deck_with_connection(
                conn,
                name or f"PG Deck Card Extra {self.token} {len(self.deck_ids)}",
            )
            conn.commit()
            self.deck_ids.append(deck_id)
            return deck_id
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def tearDown(self):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            for deck_id in self.deck_ids:
                conn.execute(
                    "DELETE FROM app.decklists WHERE id = ?",
                    (deck_id,),
                )

            conn.execute(
                "DELETE FROM app.users WHERE username IN (?, ?)",
                (self.admin_username, self.vendor_username),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def post_add(self, *, client=None, headers=True, **overrides):
        data = {
            "name": f"PG Deck Card Added {self.token}",
            "quantity": "2",
            "set_code": "",
            "collector_number": "",
            "category": "Mainboard",
            "role_tags": "",
            "finish": "normal",
            "scryfall_id": "",
            "tcgplayer_id": "",
        }
        data.update(overrides)
        if headers:
            data["csrf_token"] = self.csrf

        return (client or self.admin_client).post(
            f"/decks/{self.deck_id}/cards/add",
            data=data,
            base_url="http://localhost",
        )

    def insert_deck_card(self, deck_id=None, *, name="Old Deck Card"):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            row = conn.execute(
                """
                INSERT INTO app.deck_cards (
                    deck_id, game_id, name, quantity, finish,
                    role_tags_json, any_printing
                )
                VALUES (?, 'mtg', ?, 1, 'normal', '[]'::jsonb, FALSE)
                RETURNING id
                """,
                (deck_id or self.deck_id, name),
            ).fetchone()
            conn.commit()
            return int(row["id"])
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def fetch_card(self, card_id):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            return conn.execute(
                """
                SELECT
                    id, deck_id, game_id, name, quantity, scryfall_id,
                    set_code, collector_number, category, finish,
                    tcgplayer_id, role_tags_json, any_printing,
                    card_id, printing_id
                FROM app.deck_cards
                WHERE id = ?
                """,
                (card_id,),
            ).fetchone()
        finally:
            conn.close()

    def fetch_updated_at(self, deck_id=None):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            row = conn.execute(
                "SELECT updated_at FROM app.decklists WHERE id = ?",
                (deck_id or self.deck_id,),
            ).fetchone()
            return row["updated_at"]
        finally:
            conn.close()

    def test_add_normalizes_fields_and_marks_basic_lands_generic(self):
        response = self.post_add(
            name=" Island ",
            quantity="0",
            set_code="zzt",
            collector_number="DECKTEST",
            category="",
            role_tags="Ramp, Artifact, ramp, Draw",
            finish="foil etched",
        )

        self.assertEqual(response.status_code, 302)
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            row = conn.execute(
                """
                SELECT name, quantity, set_code, collector_number, category,
                       role_tags_json, finish, any_printing, card_id, printing_id
                FROM app.deck_cards
                WHERE deck_id = ? AND name = 'Island'
                """,
                (self.deck_id,),
            ).fetchone()
        finally:
            conn.close()

        self.assertIsNotNone(row)
        self.assertEqual(row["quantity"], 1)
        self.assertEqual(row["set_code"], "ZZT")
        self.assertEqual(row["collector_number"], "DECKTEST")
        self.assertEqual(row["category"], "Mainboard")
        self.assertEqual(row["role_tags_json"], ["Ramp", "Artifact", "Draw"])
        self.assertEqual(row["finish"], "foil")
        self.assertIs(row["any_printing"], True)
        self.assertIsNone(row["card_id"])
        self.assertIsNone(row["printing_id"])

    def test_add_resolves_known_scryfall_id_to_catalog_references(self):
        if not self.catalog:
            self.skipTest("Known Scryfall catalog fixture is unavailable")

        response = self.post_add(
            name="Submitted Name Should Be Resolved",
            scryfall_id=self.RESOLVED_SCRYFALL_ID,
        )
        self.assertEqual(response.status_code, 302)

        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            row = conn.execute(
                """
                SELECT name, scryfall_id, set_code, collector_number,
                       card_id, printing_id
                FROM app.deck_cards
                WHERE deck_id = ? AND scryfall_id = ?
                """,
                (self.deck_id, self.RESOLVED_SCRYFALL_ID),
            ).fetchone()
        finally:
            conn.close()

        self.assertIsNotNone(row)
        self.assertEqual(row["name"], self.catalog["name"])
        self.assertEqual(row["set_code"], self.catalog["set_code"])
        self.assertEqual(
            row["collector_number"], self.catalog["collector_number"]
        )
        self.assertEqual(row["card_id"], self.catalog["card_id"])
        self.assertEqual(row["printing_id"], self.catalog["printing_id"])

    def test_add_preserves_unresolved_import_identifier(self):
        placeholder = f"import-placeholder-{self.token}"
        name = f"Unresolved Deck Card {self.token}"

        response = self.post_add(name=name, scryfall_id=placeholder)
        self.assertEqual(response.status_code, 302)

        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            row = conn.execute(
                """
                SELECT scryfall_id, card_id, printing_id
                FROM app.deck_cards
                WHERE deck_id = ? AND name = ?
                """,
                (self.deck_id, name),
            ).fetchone()
        finally:
            conn.close()

        self.assertIsNotNone(row)
        self.assertEqual(row["scryfall_id"], placeholder)
        self.assertIsNone(row["card_id"])
        self.assertIsNone(row["printing_id"])

    def test_add_to_missing_deck_does_not_insert_a_card(self):
        missing_deck_id = self.deck_id + 9_000_000_000
        name = f"Must Not Be Added {self.token}"

        response = self.admin_client.post(
            f"/decks/{missing_deck_id}/cards/add",
            data={"csrf_token": self.csrf, "name": name},
            base_url="http://localhost",
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/decks", response.location)

        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            count = conn.execute(
                "SELECT count(*) AS n FROM app.deck_cards WHERE name = ?",
                (name,),
            ).fetchone()["n"]
        finally:
            conn.close()
        self.assertEqual(count, 0)

    def test_add_updates_deck_timestamp(self):
        before = self.fetch_updated_at()
        response = self.post_add(name=f"Timestamp Deck Card {self.token}")
        self.assertEqual(response.status_code, 302)
        self.assertGreaterEqual(self.fetch_updated_at(), before)

    def test_edit_updates_metadata_tags_and_catalog_references(self):
        if not self.catalog:
            self.skipTest("Known Scryfall catalog fixture is unavailable")

        card_id = self.insert_deck_card()
        before = self.fetch_updated_at()

        response = self.admin_client.post(
            f"/decks/{self.deck_id}/cards/{card_id}/edit",
            data={
                "csrf_token": self.csrf,
                "name": "Submitted Edit Name",
                "quantity": "3",
                "set_code": "",
                "collector_number": "",
                "category": "",
                "role_tags": "Ramp, Draw, Ramp",
                "finish": "etched",
                "scryfall_id": self.RESOLVED_SCRYFALL_ID,
                "tcgplayer_id": "98765",
                "any_printing": "1",
            },
            base_url="http://localhost",
        )

        self.assertEqual(response.status_code, 302)
        row = self.fetch_card(card_id)

        self.assertEqual(row["name"], self.catalog["name"])
        self.assertEqual(row["quantity"], 3)
        self.assertEqual(row["set_code"], self.catalog["set_code"])
        self.assertEqual(
            row["collector_number"], self.catalog["collector_number"]
        )
        self.assertEqual(row["category"], "Mainboard")
        self.assertEqual(row["role_tags_json"], ["Ramp", "Draw"])
        self.assertEqual(row["finish"], "foil")
        self.assertEqual(row["scryfall_id"], self.RESOLVED_SCRYFALL_ID)
        self.assertEqual(
            row["tcgplayer_id"], self.catalog["tcgplayer_id"] or "98765"
        )
        self.assertIs(row["any_printing"], True)
        self.assertEqual(row["card_id"], self.catalog["card_id"])
        self.assertEqual(row["printing_id"], self.catalog["printing_id"])
        self.assertGreaterEqual(self.fetch_updated_at(), before)

    def test_edit_empty_name_does_not_modify_existing_card(self):
        card_id = self.insert_deck_card(name="Keep This Deck Card")

        response = self.admin_client.post(
            f"/decks/{self.deck_id}/cards/{card_id}/edit",
            data={
                "csrf_token": self.csrf,
                "name": "   ",
                "quantity": "5",
                "category": "Sideboard",
            },
            base_url="http://localhost",
        )

        self.assertEqual(response.status_code, 302)
        row = self.fetch_card(card_id)
        self.assertEqual(row["name"], "Keep This Deck Card")
        self.assertEqual(row["quantity"], 1)

    def test_edit_card_from_another_deck_is_not_modified(self):
        other_card_id = self.insert_deck_card(
            self.other_deck_id,
            name="Protected Other Deck Card",
        )

        response = self.admin_client.post(
            f"/decks/{self.deck_id}/cards/{other_card_id}/edit",
            data={
                "csrf_token": self.csrf,
                "name": "Attempted Cross-Deck Edit",
                "quantity": "9",
                "category": "Sideboard",
            },
            base_url="http://localhost",
        )

        self.assertEqual(response.status_code, 302)
        row = self.fetch_card(other_card_id)
        self.assertEqual(row["deck_id"], self.other_deck_id)
        self.assertEqual(row["name"], "Protected Other Deck Card")
        self.assertEqual(row["quantity"], 1)

    def test_delete_removes_only_the_card_in_the_target_deck(self):
        target_card_id = self.insert_deck_card(name="Target Deck Card")
        other_card_id = self.insert_deck_card(
            self.other_deck_id,
            name="Other Deck Card",
        )

        wrong_target = self.admin_client.post(
            f"/decks/{self.deck_id}/cards/{other_card_id}/delete",
            data={"csrf_token": self.csrf},
            base_url="http://localhost",
        )
        self.assertEqual(wrong_target.status_code, 302)
        self.assertIsNotNone(self.fetch_card(other_card_id))

        response = self.admin_client.post(
            f"/decks/{self.deck_id}/cards/{target_card_id}/delete",
            data={"csrf_token": self.csrf},
            base_url="http://localhost",
        )
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(self.fetch_card(target_card_id))
        self.assertIsNotNone(self.fetch_card(other_card_id))

    def test_delete_updates_deck_timestamp(self):
        card_id = self.insert_deck_card()
        before = self.fetch_updated_at()

        response = self.admin_client.post(
            f"/decks/{self.deck_id}/cards/{card_id}/delete",
            data={"csrf_token": self.csrf},
            base_url="http://localhost",
        )

        self.assertEqual(response.status_code, 302)
        self.assertGreaterEqual(self.fetch_updated_at(), before)

    def test_all_three_routes_reject_missing_csrf(self):
        card_id = self.insert_deck_card()
        requests = (
            (
                f"/decks/{self.deck_id}/cards/add",
                {"name": "CSRF Add Must Fail"},
            ),
            (
                f"/decks/{self.deck_id}/cards/{card_id}/edit",
                {"name": "CSRF Edit Must Fail"},
            ),
            (
                f"/decks/{self.deck_id}/cards/{card_id}/delete",
                {},
            ),
        )

        for path, data in requests:
            with self.subTest(path=path):
                response = self.admin_client.post(
                    path,
                    data=data,
                    base_url="http://localhost",
                )
                self.assertEqual(response.status_code, 400)

        self.assertEqual(
            self.fetch_card(card_id)["name"],
            "Old Deck Card",
        )

    def test_vendor_cannot_add_deck_cards(self):
        name = f"Vendor Must Not Add Deck Card {self.token}"
        response = self.post_add(
            client=self.vendor_client,
            name=name,
        )
        self.assertEqual(response.status_code, 403)

        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            count = conn.execute(
                """
                SELECT count(*) AS n
                FROM app.deck_cards
                WHERE deck_id = ? AND name = ?
                """,
                (self.deck_id, name),
            ).fetchone()["n"]
        finally:
            conn.close()

        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
