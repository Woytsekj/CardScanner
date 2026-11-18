"""PostgreSQL integration tests for deck comparison and allocation."""

from __future__ import annotations

import os
import unittest
from uuid import uuid4

from psycopg.types.json import Jsonb

from postgres_app_db import connect_app_database
from postgres_db import close_pool, open_pool, run_migrations


@unittest.skipUnless(
    os.environ.get("CARD_SCANNER_POSTGRES_DSN")
    and os.environ.get("CARD_SCANNER_DATABASE_BACKEND") == "postgres",
    "Run with PostgreSQL backend and development DSN configured",
)
class PostgreSQLDeckComparisonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        open_pool()
        run_migrations()

        import app as app_module

        if app_module.DATABASE_BACKEND != "postgres":
            raise RuntimeError("Tests require PostgreSQL app mode.")

        cls.app_module = app_module
        cls.app_module.app.config["TESTING"] = True

    @classmethod
    def tearDownClass(cls):
        close_pool()

    def setUp(self):
        self.token = uuid4().hex
        self.username = f"pg-deck-read-{self.token}"
        self.owner = f"PGDeckOwner-{self.token}"
        self.deck_ids = []
        self.inventory_ids = []

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
                (self.username, "unused-deck-read-test-hash"),
            )
            self.deck_id = self.add_deck(conn, "Primary")
            self.other_deck_id = self.add_deck(conn, "Other")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        self.client = self.app_module.app.test_client()
        with self.client.session_transaction() as session:
            session["username"] = self.username
            session["csrf_token"] = f"csrf-{self.token}"
            session["is_admin"] = True

    def tearDown(self):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            for inventory_id in self.inventory_ids:
                conn.execute(
                    "DELETE FROM app.inventory WHERE id = ?",
                    (inventory_id,),
                )
            for deck_id in self.deck_ids:
                conn.execute(
                    "DELETE FROM app.decklists WHERE id = ?",
                    (deck_id,),
                )
            conn.execute(
                "DELETE FROM app.users WHERE username = ?",
                (self.username,),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def add_deck(self, conn, suffix):
        row = conn.execute(
            """
            INSERT INTO app.decklists (
                game_id, name, source, created_at, updated_at,
                tags_json, person_name
            )
            VALUES ('mtg', ?, 'manual', now(), now(), ?, ?)
            RETURNING id
            """,
            (f"PG comparison {suffix} {self.token}", Jsonb(["PG Test"]), self.owner),
        ).fetchone()
        deck_id = int(row["id"])
        self.deck_ids.append(deck_id)
        return deck_id

    def add_requirement(
        self, name, quantity, scryfall_id, *,
        deck_id=None, finish="normal", any_printing=False,
        card_id=None, printing_id=None, tcgplayer_id=None,
        role_tags=None,
    ):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            row = conn.execute(
                """
                INSERT INTO app.deck_cards (
                    deck_id, game_id, name, quantity, scryfall_id,
                    set_code, collector_number, category, finish,
                    tcgplayer_id, role_tags_json, any_printing,
                    card_id, printing_id
                )
                VALUES (
                    ?, 'mtg', ?, ?, ?, 'tst', '1', 'Mainboard', ?,
                    ?, ?, ?, ?, ?
                )
                RETURNING id
                """,
                (
                    deck_id or self.deck_id,
                    name,
                    quantity,
                    scryfall_id,
                    finish,
                    tcgplayer_id,
                    Jsonb(role_tags or []),
                    any_printing,
                    card_id,
                    printing_id,
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
        self, name, scryfall_id, quantity, owner=None, *,
        deck_id=None, finish="normal", for_vending=False,
        card_id=None, printing_id=None,
    ):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            row = conn.execute(
                """
                INSERT INTO app.inventory (
                    game_id, card_id, printing_id, scryfall_id, name,
                    set_code, collector_number, scan_time, quantity,
                    owner, finish, location, deck_id, for_vending
                )
                VALUES (
                    'mtg', ?, ?, ?, ?, 'tst', '1', now(), ?,
                    ?, ?, 'PG Test Storage', ?, ?
                )
                RETURNING id
                """,
                (
                    card_id,
                    printing_id,
                    scryfall_id,
                    name,
                    quantity,
                    owner or self.owner,
                    finish,
                    deck_id,
                    for_vending,
                ),
            ).fetchone()
            conn.commit()
            inventory_id = int(row["id"])
            self.inventory_ids.append(inventory_id)
            return inventory_id
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def compare(self, fuzzy=False):
        return self.app_module.deck_comparison(
            self.deck_id, self.owner, fuzzy=fuzzy
        )

    def test_postgres_deck_pages_render_and_hide_unmigrated_actions(self):
        self.add_requirement(
            "PG Rendered Card", 1, "pg-rendered-card-printing",
            role_tags=["Ramp", "Draw"],
        )

        listing = self.client.get("/decks", base_url="http://localhost")
        self.assertEqual(listing.status_code, 200, listing.get_data(as_text=True))
        listing_html = listing.get_data(as_text=True)
        self.assertIn(f"PG comparison Primary {self.token}", listing_html)
        self.assertNotIn("/decks/import", listing_html)
        self.assertNotIn(f"/decks/{self.deck_id}/delete", listing_html)

        detail = self.client.get(
            f"/decks/{self.deck_id}", base_url="http://localhost"
        )
        self.assertEqual(detail.status_code, 200, detail.get_data(as_text=True))
        detail_html = detail.get_data(as_text=True)

        self.assertIn("PG Rendered Card", detail_html)
        self.assertIn(f"/decks/{self.deck_id}/cards/add", detail_html)
        self.assertIn(f"/decks/{self.deck_id}/cards/0/edit", detail_html)
        self.assertIn(f"/decks/{self.deck_id}/cards/0/delete", detail_html)

        self.assertNotIn(f"/decks/{self.deck_id}/settings", detail_html)
        self.assertNotIn(f"/decks/{self.deck_id}/cover", detail_html)
        self.assertNotIn(f"/decks/{self.deck_id}/missing.csv", detail_html)
        self.assertNotIn(f"/decks/{self.deck_id}/move-inventory", detail_html)

    def test_deck_detail_get_does_not_assign_missing_owner_or_update_timestamp(self):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            conn.execute(
                """
                UPDATE app.decklists
                SET person_name = NULL,
                    updated_at = '2001-02-03 04:05:06+00'
                WHERE id = ?
                """,
                (self.deck_id,),
            )
            conn.commit()

            before = conn.execute(
                "SELECT person_name, updated_at FROM app.decklists WHERE id = ?",
                (self.deck_id,),
            ).fetchone()
            active_owners = [
                row["name"]
                for row in conn.execute(
                    """
                    SELECT name FROM app.people
                    WHERE active IS TRUE
                    ORDER BY LOWER(name), name
                    """
                ).fetchall()
            ]
        finally:
            conn.close()

        expected_owner = (
            "Jonathan" if "Jonathan" in active_owners
            else (active_owners[0] if active_owners else "")
        )

        response = self.client.get(
            f"/decks/{self.deck_id}", base_url="http://localhost"
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertIn(f"Deck for {expected_owner}", response.get_data(as_text=True))

        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            after = conn.execute(
                "SELECT person_name, updated_at FROM app.decklists WHERE id = ?",
                (self.deck_id,),
            ).fetchone()
        finally:
            conn.close()

        self.assertIsNone(after["person_name"])
        self.assertEqual(after["updated_at"], before["updated_at"])

    def test_allocation_priority_and_owned_totals_match_legacy_rules(self):
        self.add_requirement("PG Priority Card", 3, "pg-priority-printing")
        self.add_inventory(
            "PG Priority Card", "pg-priority-printing", 1,
            deck_id=self.deck_id,
        )
        self.add_inventory(
            "PG Priority Card", "pg-priority-printing", 1,
        )
        self.add_inventory(
            "PG Priority Card", "pg-priority-printing", 1,
            deck_id=self.other_deck_id,
        )
        self.add_inventory(
            "PG Priority Card", "pg-priority-printing", 1,
            owner="Friend", for_vending=True,
        )

        comparison, stats = self.compare()
        item = comparison[0]

        self.assertEqual(
            [source["status"] for source in item["sources"]],
            ["current", "free", "other-deck", "other-owner"],
        )
        self.assertEqual(item["assigned"], 1)
        self.assertEqual(item["free_available"], 1)
        self.assertEqual(item["other_deck_available"], 1)
        self.assertEqual(item["other_people_available"], 1)
        self.assertEqual(item["owned"], 3)
        self.assertEqual(item["missing"], 0)
        self.assertEqual(stats["owned"], 3)
        self.assertEqual(stats["assigned"], 1)

    def test_inventory_quantity_is_not_allocated_twice(self):
        self.add_requirement("PG Shared Card", 1, "pg-shared-printing")
        self.add_requirement("PG Shared Card", 1, "pg-shared-printing")
        self.add_inventory("PG Shared Card", "pg-shared-printing", 1)

        comparison, stats = self.compare()

        self.assertEqual(len(comparison), 2)
        self.assertEqual(sum(row["owned"] for row in comparison), 1)
        self.assertEqual(stats["required"], 2)
        self.assertEqual(stats["owned"], 1)
        self.assertEqual(stats["missing"], 1)

    def test_fuzzy_name_fallback_and_finish_strictness(self):
        self.add_requirement("PG Fuzzy Card", 2, "pg-deck-alt-art")
        self.add_inventory(
            "PG Fuzzy Card", "pg-inventory-alt-art", 1, finish="normal"
        )
        self.add_inventory(
            "PG Fuzzy Card", "pg-inventory-foil-art", 1, finish="foil"
        )

        strict, _ = self.compare(fuzzy=False)
        fuzzy, _ = self.compare(fuzzy=True)

        self.assertEqual(strict[0]["free_available"], 0)
        self.assertEqual(fuzzy[0]["free_available"], 1)
        self.assertEqual(fuzzy[0]["missing"], 1)
        self.assertEqual(
            [source["finish"] for source in fuzzy[0]["sources"]],
            ["normal"],
        )

    def test_generic_basic_land_matches_name_and_finish_not_other_art_names(self):
        self.add_requirement(
            "Island", 2, "pg-generic-island-art", any_printing=True
        )
        self.add_inventory("Island", "pg-binder-island-art", 1)
        self.add_inventory("Island", "pg-another-island-art", 1)
        self.add_inventory("Snow-Covered Island", "pg-snow-island", 1)
        self.add_inventory(
            "Island", "pg-foil-island", 1, finish="foil"
        )

        comparison, _ = self.compare(fuzzy=True)

        self.assertTrue(comparison[0]["any_printing"])
        self.assertEqual(comparison[0]["free_available"], 2)
        self.assertEqual(comparison[0]["missing"], 0)
        self.assertEqual(
            sum(source["status"] == "free" for source in comparison[0]["sources"]),
            2,
        )

    def test_fuzzy_mode_matches_catalog_oracle_identity_across_printings(self):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            rows = conn.execute(
                """
                WITH candidate AS (
                    SELECT p.card_id
                    FROM catalog.printings AS p
                    JOIN catalog.external_ids AS e
                      ON e.game_id = p.game_id
                     AND e.printing_id = p.printing_id
                     AND e.provider_id = 'scryfall'
                     AND e.entity_type = 'printing'
                     AND e.external_id_type = 'scryfall_id'
                    WHERE p.game_id = 'mtg'
                      AND p.card_id IS NOT NULL
                    GROUP BY p.card_id
                    HAVING COUNT(DISTINCT p.printing_id) >= 2
                    ORDER BY p.card_id
                    LIMIT 1
                )
                SELECT
                    c.card_id,
                    c.name,
                    c.metadata->>'oracle_id' AS oracle_id,
                    p.printing_id,
                    e.external_id AS scryfall_id
                FROM candidate AS x
                JOIN catalog.cards AS c
                  ON c.game_id = 'mtg'
                 AND c.card_id = x.card_id
                JOIN catalog.printings AS p
                  ON p.game_id = c.game_id
                 AND p.card_id = c.card_id
                JOIN catalog.external_ids AS e
                  ON e.game_id = p.game_id
                 AND e.printing_id = p.printing_id
                 AND e.provider_id = 'scryfall'
                 AND e.entity_type = 'printing'
                 AND e.external_id_type = 'scryfall_id'
                ORDER BY p.printing_id
                LIMIT 2
                """
            ).fetchall()
        finally:
            conn.close()

        if len(rows) != 2 or not rows[0]["oracle_id"]:
            self.skipTest("No two-printing Oracle-linked catalog card found.")

        first, second = rows
        self.add_requirement(
            first["name"], 1, first["scryfall_id"],
            card_id=first["card_id"], printing_id=first["printing_id"],
        )
        self.add_inventory(
            second["name"], second["scryfall_id"], 1,
            card_id=second["card_id"], printing_id=second["printing_id"],
        )

        strict, _ = self.compare(fuzzy=False)
        fuzzy, _ = self.compare(fuzzy=True)

        self.assertEqual(strict[0]["free_available"], 0)
        self.assertEqual(fuzzy[0]["free_available"], 1)
        self.assertEqual(fuzzy[0]["missing"], 0)

    def test_catalog_tcgcsv_mapping_supplies_missing_tcgplayer_id(self):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            row = conn.execute(
                """
                SELECT
                    c.card_id,
                    c.name,
                    p.printing_id,
                    e.external_id AS scryfall_id,
                    m.target_external_product_id
                FROM catalog.printing_product_mappings AS m
                JOIN catalog.printings AS p
                  ON p.game_id = m.game_id
                 AND p.printing_id = m.printing_id
                JOIN catalog.cards AS c
                  ON c.game_id = p.game_id
                 AND c.card_id = p.card_id
                JOIN catalog.external_ids AS e
                  ON e.game_id = p.game_id
                 AND e.printing_id = p.printing_id
                 AND e.provider_id = 'scryfall'
                 AND e.entity_type = 'printing'
                 AND e.external_id_type = 'scryfall_id'
                WHERE m.game_id = 'mtg'
                  AND m.source_provider_id = 'scryfall'
                  AND m.target_provider_id = 'tcgcsv'
                  AND m.printing_id IS NOT NULL
                ORDER BY p.printing_id
                LIMIT 1
                """
            ).fetchone()
        finally:
            conn.close()

        if not row:
            self.skipTest("No resolved TCGCSV printing mapping found.")

        self.add_requirement(
            row["name"], 1, row["scryfall_id"],
            card_id=row["card_id"], printing_id=row["printing_id"],
        )

        comparison, _ = self.compare()

        self.assertEqual(
            comparison[0]["tcgplayer_id"],
            row["target_external_product_id"],
        )


if __name__ == "__main__":
    unittest.main()
