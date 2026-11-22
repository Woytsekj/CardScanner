"""PostgreSQL integration tests for desktop and mobile scanner persistence."""

from __future__ import annotations

import os
import unittest
from uuid import uuid4

from postgres_app_db import connect_app_database
from postgres_db import close_pool, open_pool, run_migrations
from postgres_scanner import (
    fetch_active_people,
    fetch_catalog_card_names,
    fetch_catalog_set_codes,
    fetch_location_options,
    fetch_mobile_authorized_people,
    fetch_mobile_catalog_search_results,
    fetch_scanner_options,
    insert_or_increment_scan,
    resolve_printing_price_mapping,
)


@unittest.skipUnless(
    os.environ.get("CARD_SCANNER_POSTGRES_DSN")
    and os.environ.get("CARD_SCANNER_DATABASE_BACKEND") == "postgres",
    "Run with the PostgreSQL backend and development DSN configured",
)
class PostgreSQLScannerPersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        open_pool()
        run_migrations()

        import app as app_module

        if app_module.DATABASE_BACKEND != "postgres":
            raise RuntimeError("Scanner persistence tests require PostgreSQL app mode")
        cls.app_module = app_module

    @classmethod
    def tearDownClass(cls):
        close_pool()

    def setUp(self):
        self.token = uuid4().hex
        self.prefix = f"PGScanner{self.token}"
        self.owner = f"PG Scanner Owner {self.token}"
        self.username = f"pg-scanner-user-{self.token}"
        self.scan_id_one = f"scanner-{self.token}-one"
        self.scan_id_two = f"scanner-{self.token}-two"
        self.catalog_name = f"{self.prefix} Catalog Card"
        self.set_code = f"p{self.token[:7]}".lower()
        self.scryfall_id = str(uuid4())

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
                VALUES (?, ?, FALSE, 'user', FALSE, TRUE, ?)
                """,
                (self.username, "unused-scanner-test-password-hash", self.person_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _add_catalog_printing(self, conn):
        card = conn.execute(
            """
            INSERT INTO catalog.cards (game_id, name)
            VALUES ('mtg', ?)
            RETURNING card_id
            """,
            (self.catalog_name,),
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
                metadata
            )
            VALUES (
                'mtg', ?, ?, '17', 'rare',
                '{"name":"Catalog Card","finishes":["nonfoil","foil"],"tcgplayer_id":"987654"}'::jsonb
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
            VALUES ('mtg', 'scryfall', 'printing', 'scryfall_id', ?, ?)
            """,
            (self.scryfall_id, self.printing_id),
        )
        conn.execute(
            """
            UPDATE catalog.game_catalog_state
            SET revision = revision + 1, updated_at = now()
            WHERE game_id = 'mtg'
            """
        )

    def tearDown(self):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            conn.execute(
                "DELETE FROM app.inventory WHERE name LIKE ?",
                (self.prefix + "%",),
            )
            conn.execute(
                "DELETE FROM app.scan_log WHERE scan_id LIKE ?",
                (f"scanner-{self.token}-%",),
            )

            conn.execute(
                "DELETE FROM catalog.external_ids WHERE external_id = ?",
                (self.scryfall_id,),
            )
            printing_id = getattr(self, "printing_id", None)
            if printing_id is not None:
                conn.execute(
                    "DELETE FROM catalog.printings WHERE printing_id = ? AND game_id = 'mtg'",
                    (printing_id,),
                )
            set_id = getattr(self, "set_id", None)
            if set_id is not None:
                conn.execute(
                    "DELETE FROM catalog.sets WHERE set_id = ? AND game_id = 'mtg'",
                    (set_id,),
                )
            card_id = getattr(self, "card_id", None)
            if card_id is not None:
                conn.execute(
                    "DELETE FROM catalog.cards WHERE card_id = ? AND game_id = 'mtg'",
                    (card_id,),
                )
            if printing_id is not None or set_id is not None or card_id is not None:
                conn.execute(
                    """
                    UPDATE catalog.game_catalog_state
                    SET revision = revision + 1, updated_at = now()
                    WHERE game_id = 'mtg'
                    """
                )

            conn.execute(
                "DELETE FROM app.users WHERE username = ?",
                (self.username,),
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

    def test_scanner_options_include_active_people_accounts_and_locations(self):
        card_name = f"{self.prefix} Location Card"
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            conn.execute(
                """
                INSERT INTO app.inventory (
                    game_id, name, owner, person_id, location, quantity
                )
                VALUES ('mtg', ?, ?, ?, 'Scanner Storage Box', 1)
                """,
                (card_name, self.owner, self.person_id),
            )
            conn.commit()

            self.assertIn(self.owner, fetch_active_people(conn))

            locations = fetch_location_options(conn, self.owner)
            self.assertIn(
                {"id": "Scanner Storage Box", "name": "Scanner Storage Box"},
                locations,
            )
            self.assertTrue(any(item["id"].casefold() == "free" for item in locations))

            options = fetch_scanner_options(conn)
            self.assertIn(
                {
                    "id": self.person_id,
                    "person_id": self.person_id,
                    "name": self.owner,
                    "account_id": self.username,
                    "account_name": self.username,
                },
                options["people"],
            )
            self.assertEqual(options["people"], options["persons"])
            self.assertIn(
                {"id": self.username, "name": self.username},
                options["accounts"],
            )
            self.assertEqual(options["locations"], options["storage_locations"])
        finally:
            conn.close()

    def test_mobile_person_options_are_scoped_to_linked_account(self):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            user = {
                "username": self.username,
                "person_id": self.person_id,
                "is_admin": False,
                "is_superadmin": False,
                "role": "user",
            }
            visible = fetch_mobile_authorized_people(conn, user, is_superadmin=False)
            self.assertEqual(len(visible), 1)
            self.assertEqual(visible[0]["id"], self.person_id)
            self.assertEqual(visible[0]["name"], self.owner)
            self.assertEqual(visible[0]["account_id"], self.username)

            admin_visible = fetch_mobile_authorized_people(
                conn, user, is_admin=True
            )
            self.assertIn(self.owner, [row["name"] for row in admin_visible])
        finally:
            conn.close()

    def test_unknown_printing_mapping_returns_empty_without_network_lookup(self):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            result = resolve_printing_price_mapping(
                conn, f"missing-scryfall-{self.token}"
            )
            self.assertIsNone(result["tcgplayer_id"])
            self.assertEqual(result["finishes"], [])
            self.assertIsNone(result["name"])
        finally:
            conn.close()

    def test_mobile_catalog_search_and_footer_use_postgres_catalog(self):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            self._add_catalog_printing(conn)
            # The mobile cache helper opens its own connection, so fixture data
            # must be committed before checking that cross-connection read path.
            conn.commit()
            revision, names = fetch_catalog_card_names(conn)
            self.assertGreaterEqual(revision, 1)
            self.assertIn(self.catalog_name, names)

            set_revision, set_codes = fetch_catalog_set_codes(conn)
            self.assertEqual(set_revision, revision)
            self.assertIn(self.set_code, set_codes)

            mapping = resolve_printing_price_mapping(conn, self.scryfall_id)
            self.assertEqual(mapping["tcgplayer_id"], "987654")
            self.assertEqual(mapping["finishes"], ["nonfoil", "foil"])

            rows = fetch_mobile_catalog_search_results(
                conn,
                self.catalog_name,
                self.set_code,
                "17",
                limit=10,
            )
            matching = [row for row in rows if row["scryfall_id"] == self.scryfall_id]
            self.assertEqual(len(matching), 1)
            self.assertEqual(matching[0]["name"], self.catalog_name)
            self.assertEqual(matching[0]["set_code"], self.set_code)
            self.assertEqual(matching[0]["collector_number"], "17")
            self.assertEqual(matching[0]["tcgplayer_id"], "987654")
            self.assertEqual(matching[0]["finishes"], ["nonfoil", "foil"])

            # Exercise the same cache-backed paths used by the mobile OCR route.
            self.assertIn(
                self.catalog_name,
                self.app_module._scanner_mobile_cached_card_names(),
            )
            footer = self.app_module._scanner_mobile_parse_footer_metadata(
                f"R 017\n{self.set_code.upper()} • EN"
            )
            self.assertEqual(footer["set_code"], self.set_code)
            self.assertEqual(footer["collector_number"], "17")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def test_scan_increments_matching_row_and_duplicate_retry_is_idempotent(self):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            first = insert_or_increment_scan(
                conn,
                {
                    "scryfall_id": f"unknown-printing-{self.token}",
                    "name": f"{self.prefix} Scanned Card",
                    "owner": self.owner,
                    "person_id": self.person_id,
                    "account_id": self.username,
                    "quantity": 2,
                    "finish": "normal",
                    "location": "Scanner Tray",
                    "scan_id": self.scan_id_one,
                    "price": "1.25",
                },
                parse_quantity=self.app_module.parse_qty,
                normalize_finish=self.app_module.normalize_finish,
            )
            self.assertTrue(first["success"])
            self.assertEqual(first["quantity"], 2)

            second = insert_or_increment_scan(
                conn,
                {
                    "scryfall_id": f"unknown-printing-{self.token}",
                    "name": f"{self.prefix} Scanned Card",
                    "owner": self.owner,
                    "person_id": self.person_id,
                    "account_id": self.username,
                    "quantity": 3,
                    "finish": "normal",
                    "location": "Scanner Tray",
                    "scan_id": self.scan_id_two,
                    "price": "1.50",
                },
                parse_quantity=self.app_module.parse_qty,
                normalize_finish=self.app_module.normalize_finish,
            )
            self.assertTrue(second["success"])
            self.assertEqual(second["id"], first["id"])
            self.assertEqual(second["quantity"], 5)

            duplicate = insert_or_increment_scan(
                conn,
                {
                    "scryfall_id": f"unknown-printing-{self.token}",
                    "name": f"{self.prefix} Scanned Card",
                    "owner": self.owner,
                    "person_id": self.person_id,
                    "account_id": self.username,
                    "quantity": 3,
                    "finish": "normal",
                    "location": "Scanner Tray",
                    "scan_id": self.scan_id_two,
                    "price": "1.50",
                },
                parse_quantity=self.app_module.parse_qty,
                normalize_finish=self.app_module.normalize_finish,
            )
            self.assertTrue(duplicate["duplicate"])
            self.assertEqual(duplicate["id"], first["id"])
            self.assertEqual(duplicate["quantity"], 5)

            rows = conn.execute(
                """
                SELECT id, quantity, owner, person_id, location, finish
                FROM app.inventory
                WHERE name = ?
                """,
                (f"{self.prefix} Scanned Card",),
            ).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(int(rows[0]["quantity"]), 5)
            self.assertEqual(rows[0]["owner"], self.owner)
            self.assertEqual(int(rows[0]["person_id"]), self.person_id)
            self.assertEqual(rows[0]["location"], "Scanner Tray")
            self.assertEqual(rows[0]["finish"], "normal")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def test_scan_rejects_unknown_owner(self):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            with self.assertRaisesRegex(ValueError, "not an active person"):
                insert_or_increment_scan(
                    conn,
                    {
                        "scryfall_id": f"unknown-printing-{self.token}",
                        "name": f"{self.prefix} Invalid Owner Card",
                        "owner": "No Such Active Person",
                    },
                    parse_quantity=self.app_module.parse_qty,
                    normalize_finish=self.app_module.normalize_finish,
                )
            conn.rollback()
            rows = conn.execute(
                "SELECT COUNT(*) AS count FROM app.inventory WHERE name LIKE ?",
                (self.prefix + "%",),
            ).fetchone()
            self.assertEqual(int(rows["count"]), 0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
