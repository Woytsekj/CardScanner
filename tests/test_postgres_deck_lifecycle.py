"""PostgreSQL integration tests for deck lifecycle and inventory allocation."""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from uuid import uuid4

from postgres_app_db import connect_app_database
from postgres_db import close_pool, open_pool, run_migrations
from postgres_deck_lifecycle import (
    assign_free_to_deck,
    delete_deck,
    move_inventory_to_deck,
    update_deck_settings,
)


@unittest.skipUnless(
    os.environ.get("CARD_SCANNER_POSTGRES_DSN")
    and os.environ.get("CARD_SCANNER_DATABASE_BACKEND") == "postgres",
    "Run with the PostgreSQL backend and development DSN configured",
)
class PostgreSQLDeckLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        open_pool()
        run_migrations()
        import app as app_module

        if app_module.DATABASE_BACKEND != "postgres":
            raise RuntimeError("Deck lifecycle tests require PostgreSQL app mode")
        cls.app_module = app_module
        cls.app_module.app.config["TESTING"] = True

    @classmethod
    def tearDownClass(cls):
        close_pool()

    def setUp(self):
        self.token = uuid4().hex
        self.prefix = f"PGDeckLife{self.token}"
        self.owner = f"PG Deck Owner {self.token}"
        self.admin_username = f"pg-deck-life-admin-{self.token}"
        self.csrf_token = f"csrf-deck-life-{self.token}"
        self.now = datetime.now(timezone.utc)

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
                    self.admin_username,
                    "unused-deck-lifecycle-password-hash",
                    self.person_id,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        self.client = self.app_module.app.test_client()
        with self.client.session_transaction() as session:
            session["username"] = self.admin_username
            session["csrf_token"] = self.csrf_token

        self.deck_id = self.create_deck(f"{self.prefix} Main Deck")
        self.other_deck_id = self.create_deck(f"{self.prefix} Other Deck")

    def create_deck(self, name, *, cover_filename=None):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            row = conn.execute(
                """
                INSERT INTO app.decklists (
                    game_id, name, source, created_at, updated_at,
                    person_id, person_name, cover_filename
                )
                VALUES ('mtg', ?, 'manual', ?, ?, ?, ?, ?)
                RETURNING id
                """,
                (
                    name,
                    self.now,
                    self.now,
                    self.person_id,
                    self.owner,
                    cover_filename,
                ),
            ).fetchone()
            conn.commit()
            return int(row["id"])
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def add_requirement(
        self, deck_id, name, quantity, *, scryfall_id=None,
        set_code="TST", collector_number="1", finish="normal",
        any_printing=False,
    ):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            row = conn.execute(
                """
                INSERT INTO app.deck_cards (
                    deck_id, game_id, name, quantity, scryfall_id,
                    set_code, collector_number, finish, any_printing
                )
                VALUES (?, 'mtg', ?, ?, ?, ?, ?, ?, ?)
                RETURNING id
                """,
                (
                    deck_id, name, quantity, scryfall_id,
                    set_code, collector_number, finish, any_printing,
                ),
            ).fetchone()
            conn.commit()
            return int(row["id"])
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def add_inventory(
        self, name, quantity, *, deck_id=None, scryfall_id=None,
        set_code="TST", collector_number="1", finish="normal",
        location="Deck Box", for_vending=False,
    ):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            row = conn.execute(
                """
                INSERT INTO app.inventory (
                    game_id, name, scryfall_id, set_code,
                    collector_number, finish, price, quantity, owner,
                    person_id, location, deck_id, for_vending
                )
                VALUES (
                    'mtg', ?, ?, ?, ?, ?, '1.25', ?, ?, ?, ?, ?, ?
                )
                RETURNING id
                """,
                (
                    name, scryfall_id, set_code, collector_number, finish,
                    quantity, self.owner, self.person_id, location,
                    deck_id, for_vending,
                ),
            ).fetchone()
            conn.commit()
            return int(row["id"])
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def rows_by_name(self, name):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            return conn.execute(
                """
                SELECT id, name, quantity, owner, person_id, location,
                       deck_id, for_vending, finish, scryfall_id,
                       set_code, collector_number
                FROM app.inventory
                WHERE name = ?
                ORDER BY id
                """,
                (name,),
            ).fetchall()
        finally:
            conn.close()

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
                "DELETE FROM app.users WHERE username = ?",
                (self.admin_username,),
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

    def test_update_settings_route_updates_owner_and_json_tags(self):
        response = self.client.post(
            f"/decks/{self.deck_id}/settings",
            data={
                "name": f"{self.prefix} Renamed",
                "deck_tags": "Commander, Test Night",
                "person_name": self.owner,
            },
            headers={"X-CSRFToken": self.csrf_token},
        )
        self.assertEqual(response.status_code, 302)

        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            deck = conn.execute(
                "SELECT name, tags_json, person_id, person_name FROM app.decklists WHERE id = ?",
                (self.deck_id,),
            ).fetchone()
            self.assertEqual(deck["name"], f"{self.prefix} Renamed")
            self.assertEqual(deck["tags_json"], ["Commander", "Test Night"])
            self.assertEqual(int(deck["person_id"]), self.person_id)
            self.assertEqual(deck["person_name"], self.owner)
        finally:
            conn.close()

    def test_assign_free_splits_inventory_and_preserves_physical_row(self):
        card_name = f"{self.prefix} Free Card"
        self.add_requirement(self.deck_id, card_name, 3)
        source_id = self.add_inventory(
            card_name, 5, location="Blue Binder", for_vending=True
        )

        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            result = assign_free_to_deck(conn, self.deck_id)
            self.assertEqual(result["status"], "assigned")
            self.assertEqual(result["moved"], 3)
            self.assertEqual(result["requested"], 3)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        rows = self.rows_by_name(card_name)
        self.assertEqual(len(rows), 2)
        source = next(row for row in rows if int(row["id"]) == source_id)
        allocated = next(row for row in rows if int(row["id"]) != source_id)

        self.assertEqual(source["quantity"], 2)
        self.assertIsNone(source["deck_id"])
        self.assertTrue(source["for_vending"])
        self.assertEqual(source["location"], "Blue Binder")

        self.assertEqual(allocated["quantity"], 3)
        self.assertEqual(int(allocated["deck_id"]), self.deck_id)
        self.assertFalse(allocated["for_vending"])
        self.assertEqual(allocated["location"], "Blue Binder")
        self.assertEqual(allocated["owner"], self.owner)
        self.assertEqual(int(allocated["person_id"]), self.person_id)

    def test_move_inventory_splits_source_and_allocates_requested_quantity(self):
        card_name = f"{self.prefix} Moved Card"
        sid = f"unresolved-{self.token}"
        self.add_requirement(
            self.deck_id, card_name, 2, scryfall_id=sid,
            collector_number="22",
        )
        source_id = self.add_inventory(
            card_name, 4, deck_id=self.other_deck_id,
            scryfall_id=sid, collector_number="22",
            location="Red Binder", for_vending=True,
        )

        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            result = move_inventory_to_deck(
                conn, self.deck_id, source_id, 2
            )
            self.assertEqual(result["status"], "moved")
            self.assertEqual(result["quantity"], 2)
            self.assertEqual(result["previous_deck_name"], f"{self.prefix} Other Deck")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        rows = self.rows_by_name(card_name)
        self.assertEqual(len(rows), 2)
        source = next(row for row in rows if int(row["id"]) == source_id)
        moved = next(row for row in rows if int(row["id"]) != source_id)
        self.assertEqual(source["quantity"], 2)
        self.assertEqual(int(source["deck_id"]), self.other_deck_id)
        self.assertTrue(source["for_vending"])
        self.assertEqual(moved["quantity"], 2)
        self.assertEqual(int(moved["deck_id"]), self.deck_id)
        self.assertFalse(moved["for_vending"])
        self.assertEqual(moved["location"], "Red Binder")
        self.assertEqual(moved["scryfall_id"], sid)

    def test_delete_deck_releases_inventory_and_returns_cover(self):
        card_name = f"{self.prefix} Assigned Card"
        self.add_requirement(self.deck_id, card_name, 1)
        self.add_inventory(
            card_name, 4, deck_id=self.deck_id,
            location="Commander Binder",
        )
        cover = f"{uuid4().hex}.png"
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            conn.execute(
                "UPDATE app.decklists SET cover_filename = ? WHERE id = ?",
                (cover, self.deck_id),
            )
            result = delete_deck(conn, self.deck_id)
            self.assertEqual(result["status"], "deleted")
            self.assertEqual(result["moved_quantity"], 4)
            self.assertEqual(result["cover_filename"], cover)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        rows = self.rows_by_name(card_name)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["deck_id"])
        self.assertEqual(rows[0]["quantity"], 4)
        self.assertEqual(rows[0]["location"], "Commander Binder")

        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            self.assertIsNone(
                conn.execute(
                    "SELECT id FROM app.decklists WHERE id = ?",
                    (self.deck_id,),
                ).fetchone()
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM app.deck_cards WHERE deck_id = ?",
                    (self.deck_id,),
                ).fetchone()["n"],
                0,
            )
        finally:
            conn.close()

    def test_deck_write_routes_require_csrf(self):
        response = self.client.post(
            f"/decks/{self.deck_id}/settings",
            data={
                "name": f"{self.prefix} No CSRF",
                "deck_tags": "",
                "person_name": self.owner,
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("CSRF", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
