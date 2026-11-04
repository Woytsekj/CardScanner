"""Regression tests for deck ownership, inventory allocation, import finishes, and fuzzy identity."""
from __future__ import annotations

import io
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

_IMPORT_DATA_DIR = tempfile.mkdtemp(prefix="cardscanner-deck-redesign-import-")
os.environ["DATA_DIR"] = _IMPORT_DATA_DIR
os.environ["REQUIRE_LOGIN"] = "1"
os.environ["SESSION_SECRET"] = "unit-test-session-secret-0123456789-this-is-not-production"
os.environ["SCANNER_API_KEY"] = "unit-test-scanner-api-key-0123456789-not-production"
os.environ["ADMIN_USERNAME"] = "ci-admin"
os.environ["ADMIN_PASSWORD"] = "unit-test-admin-password-0123456789"
os.environ["TRUSTED_HOSTS"] = "localhost,127.0.0.1"
os.environ["LOCAL_HOSTS"] = "localhost,127.0.0.1"
os.environ["PUBLIC_HOSTNAME"] = "localhost"
os.environ["PUBLIC_HTTPS"] = "0"

import app as app_module  # noqa: E402


class DeckImportFinishTests(unittest.TestCase):
    def test_archidekt_keeps_normal_and_foil_rows_separate(self) -> None:
        payload = {"cards": [
            {"quantity": 1, "isFoil": False, "card": {"name": "Psychosis Crawler", "uid": "11111111-1111-4111-8111-111111111111", "edition": {"editionCode": "MOC", "collectorNumber": "123"}}},
            {"quantity": 2, "isFoil": True, "card": {"name": "Psychosis Crawler", "uid": "11111111-1111-4111-8111-111111111111", "edition": {"editionCode": "MOC", "collectorNumber": "123"}}},
        ]}
        cards = app_module.normalize_archidekt_cards(payload)
        self.assertEqual([(c["finish"], c["quantity"]) for c in cards], [("normal", 1), ("foil", 2)])

    def test_archidekt_foil_modifier_is_recognized(self) -> None:
        payload = {"cards": [
            {"quantity": 1, "modifiers": ["Foil"], "card": {"name": "Sol Ring"}},
            {"quantity": 1, "modifiers": ["Full Art"], "card": {"name": "Sol Ring"}},
        ]}
        cards = app_module.normalize_archidekt_cards(payload)
        self.assertEqual(sorted((c["finish"], c["quantity"]) for c in cards), [("foil", 1), ("normal", 1)])

    def test_moxfield_string_false_is_not_treated_as_foil(self) -> None:
        payload = {"mainboard": {
            "normal": {"quantity": 1, "isFoil": "false", "card": {"name": "Sol Ring", "scryfall_id": "11111111-1111-4111-8111-111111111111", "set": "CMM", "cn": "390"}},
            "foil": {"quantity": 2, "isFoil": "true", "card": {"name": "Sol Ring", "scryfall_id": "11111111-1111-4111-8111-111111111111", "set": "CMM", "cn": "390"}},
        }}
        cards, _ = app_module.normalize_moxfield_deck(payload)
        self.assertEqual([(c["finish"], c["quantity"]) for c in cards], [("normal", 1), ("foil", 2)])

    def test_csv_explicit_false_overrides_finish_column(self) -> None:
        cards, _ = app_module.parse_deck_csv("Name,Quantity,Finish,Foil\nSol Ring,1,Foil,false\n")
        self.assertEqual(cards[0]["finish"], "normal")


class DeckRedesignDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="cardscanner-deck-redesign-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        app_module.DB_INV = root / "inventory.db"
        app_module.DB_ART = root / "art.db"
        app_module.PRICES_FILE = root / "prices.json"
        app_module.PRICE_META_FILE = root / "price-meta.json"
        app_module._PRICE_CACHE = {"key": None, "prices": {}}
        app_module._AUTH_READY = False
        app_module.migrate_db()
        self.client = app_module.app.test_client()
        self.csrf = "unit-test-csrf-token-012345678901234567890"
        with self.client.session_transaction() as session:
            session["username"] = "ci-admin"
            session["csrf_token"] = self.csrf
            session["is_admin"] = True

    def connect(self):
        return app_module.connect_inventory()

    def add_deck(self, name: str, owner: str = "Jonathan") -> int:
        now = datetime.now(timezone.utc).isoformat()
        conn = self.connect()
        cur = conn.execute(
            "INSERT INTO decklists(name,source,created_at,updated_at,person_name) VALUES(?,?,?,?,?)",
            (name, "manual", now, now, owner),
        )
        deck_id = int(cur.lastrowid)
        conn.commit()
        conn.close()
        return deck_id

    def add_requirement(self, deck_id: int, name: str = "Psychosis Crawler", quantity: int = 4, sid: str = "deck-printing") -> None:
        conn = self.connect()
        conn.execute(
            "INSERT INTO deck_cards(deck_id,name,quantity,scryfall_id,set_code,collector_number,category,finish) VALUES(?,?,?,?,?,?,?,?)",
            (deck_id, name, quantity, sid, "MOC", "123", "Mainboard", "normal"),
        )
        conn.commit()
        conn.close()

    def add_inventory(self, owner: str, *, name: str = "Psychosis Crawler", sid: str = "deck-printing", deck_id: int | None = None, quantity: int = 1, finish: str = "normal") -> int:
        conn = self.connect()
        cur = conn.execute(
            """INSERT INTO inventory(scryfall_id,name,set_code,rarity,price,owner,quantity,finish,
               collector_number,location,deck_id,for_vending) VALUES(?,?,?,?,?,?,?,?,?,?,?,0)""",
            (sid, name, "MOC", "Mythic", "5.00", owner, quantity, finish, "123", "Binder A", deck_id),
        )
        item_id = int(cur.lastrowid)
        conn.commit()
        conn.close()
        return item_id

    def test_source_results_are_complete_and_priority_ordered(self) -> None:
        current = self.add_deck("Current deck")
        other = self.add_deck("Older deck")
        self.add_requirement(current)
        self.add_inventory("Jonathan", deck_id=current)
        self.add_inventory("Jonathan", deck_id=None)
        self.add_inventory("Jonathan", deck_id=other)
        self.add_inventory("Friend", deck_id=None)

        comparison, stats = app_module.deck_comparison(current, "ignored")
        item = comparison[0]
        self.assertEqual([s["status"] for s in item["sources"]], ["current", "free", "other-deck", "other-owner"])
        self.assertEqual(item["missing"], 0, "blue and purple inventory copies must not be mislabeled as nobody-owns-it BUY rows")
        self.assertEqual(item["other_deck_available"], 1)
        self.assertEqual(item["other_people_available"], 1)
        self.assertEqual(stats["owned"], 3, "Jonathan's blue copy counts as owned; the purple copy does not")

    def test_fuzzy_matching_falls_back_to_name_when_inventory_metadata_is_missing(self) -> None:
        current = self.add_deck("Crawler deck")
        self.add_requirement(current, sid="11111111-1111-4111-8111-111111111111")
        self.add_inventory("Jonathan", sid="22222222-2222-4222-8222-222222222222", quantity=1)
        comparison, _ = app_module.deck_comparison(current, "Jonathan", fuzzy=True)
        self.assertEqual(comparison[0]["free_available"], 1)
        self.assertEqual(comparison[0]["missing"], 3)

    def test_moving_blue_copy_keeps_previous_deck_requirement_and_marks_it_blue(self) -> None:
        previous = self.add_deck("Previous deck")
        current = self.add_deck("Current deck")
        self.add_requirement(previous, quantity=1)
        self.add_requirement(current, quantity=1)
        inventory_id = self.add_inventory("Jonathan", deck_id=previous, quantity=1)

        response = self.client.post(
            f"/decks/{current}/move-inventory",
            data={"csrf_token": self.csrf, "inventory_id": str(inventory_id), "quantity": "1"},
            base_url="http://localhost",
        )
        self.assertEqual(response.status_code, 302)
        conn = self.connect()
        inventory_row = conn.execute("SELECT deck_id,owner,location FROM inventory WHERE id=?", (inventory_id,)).fetchone()
        previous_requirement = conn.execute("SELECT COUNT(*) FROM deck_cards WHERE deck_id=?", (previous,)).fetchone()[0]
        conn.close()
        self.assertEqual(inventory_row["deck_id"], current)
        self.assertEqual(inventory_row["owner"], "Jonathan")
        self.assertEqual(inventory_row["location"], "Binder A")
        self.assertEqual(previous_requirement, 1, "moving a physical copy must not delete the old deck's requested card")

        old_comparison, _ = app_module.deck_comparison(previous, "Jonathan")
        self.assertEqual(old_comparison[0]["sources"][0]["status"], "other-deck")

    def test_inventory_csv_import_foil_flag_overrides_finish_column(self) -> None:
        csv_data = (
            "Name,Set code,Collector number,Foil,Finish,Quantity\n"
            "Foil Test Card,MOC,123,true,normal,1\n"
            "Nonfoil Test Card,MOC,124,false,foil,1\n"
        )
        response = self.client.post(
            "/import_csv",
            data={"csrf_token": self.csrf, "file": (io.BytesIO(csv_data.encode()), "manabox.csv")},
            content_type="multipart/form-data",
            base_url="http://localhost",
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        conn = self.connect()
        rows = conn.execute(
            "SELECT name,finish FROM inventory WHERE name IN ('Foil Test Card','Nonfoil Test Card') ORDER BY name"
        ).fetchall()
        conn.close()
        self.assertEqual([(row["name"], row["finish"]) for row in rows],
                         [("Foil Test Card", "foil"), ("Nonfoil Test Card", "normal")])

    def test_basic_land_requirement_matches_any_art_without_global_fuzzy_mode(self) -> None:
        deck_id = self.add_deck("Basic land deck")
        conn = self.connect()
        conn.execute(
            """INSERT INTO deck_cards(deck_id,name,quantity,scryfall_id,set_code,collector_number,category,finish,any_printing)
               VALUES(?,?,?,?,?,?,?,?,1)""",
            (deck_id, "Island", 3, "archidekt-placeholder-art", "M21", "265", "Mainboard", "normal"),
        )
        conn.commit()
        conn.close()
        self.add_inventory("Jonathan", name="Island", sid="binder-island-art", quantity=3)

        comparison, _ = app_module.deck_comparison(deck_id, "Jonathan", fuzzy=False)
        self.assertTrue(comparison[0]["any_printing"])
        self.assertEqual(comparison[0]["free_available"], 3)
        self.assertEqual(comparison[0]["missing"], 0)
        mismatched_land = {"name": "Snow-Covered Island", "scryfall_id": "snow-island", "finish": "normal", "oracle_id": "shared-oracle-test"}
        generic_island = {"name": "Island", "scryfall_id": "island-placeholder", "finish": "normal", "any_printing": 1, "card_oracle_id": "shared-oracle-test"}
        self.assertFalse(app_module._deck_inventory_matches(generic_island, mismatched_land, fuzzy=True))
        foil_island = dict(mismatched_land, name="Island", finish="foil")
        self.assertFalse(app_module._deck_inventory_matches(generic_island, foil_island, fuzzy=True))

    def test_imported_basic_lands_are_saved_as_generic_printing_requirements(self) -> None:
        deck_id = app_module.save_deck(
            "Archidekt Basics", "archidekt-json", None,
            [{"name": "Island", "quantity": 8, "scryfall_id": "placeholder-island-art",
              "set_code": "M21", "collector_number": "265", "category": "Mainboard", "finish": "normal"}],
            person_name="Jonathan",
        )
        conn = self.connect()
        row = conn.execute("SELECT any_printing FROM deck_cards WHERE deck_id=?", (deck_id,)).fetchone()
        conn.close()
        self.assertEqual(row[0], 1)

    def test_existing_basic_land_entries_are_backfilled_once(self) -> None:
        deck_id = self.add_deck("Legacy basics")
        conn = self.connect()
        cur = conn.execute(
            "INSERT INTO deck_cards(deck_id,name,quantity,finish,any_printing) VALUES(?,?,?,?,0)",
            (deck_id, "Snow-Covered Forest", 4, "normal"),
        )
        card_id = int(cur.lastrowid)
        conn.execute("DELETE FROM app_migrations WHERE name='deck_basic_lands_any_printing_v1'")
        conn.commit()
        conn.close()

        app_module.migrate_db()
        conn = self.connect()
        row = conn.execute("SELECT any_printing FROM deck_cards WHERE id=?", (card_id,)).fetchone()
        conn.close()
        self.assertEqual(row[0], 1)

    def test_basic_land_name_detection_covers_snow_basics_and_wastes(self) -> None:
        for name in ("Plains", "Island", "Swamp", "Mountain", "Forest", "Wastes",
                     "Snow-Covered Plains", "Snow-Covered Island", "Snow-Covered Swamp",
                     "Snow-Covered Mountain", "Snow-Covered Forest"):
            self.assertTrue(app_module._is_basic_land_name(name), name)
        self.assertFalse(app_module._is_basic_land_name("Gaea's Cradle"))

    def test_fuzzy_mode_matches_other_art_but_keeps_foil_finish_strict(self) -> None:
        deck_id = self.add_deck("Finish-sensitive fuzzy deck")
        self.add_requirement(deck_id, name="Sol Ring", quantity=1, sid="archidekt-sol-ring-art")
        self.add_inventory("Jonathan", name="Sol Ring", sid="owned-normal-art", finish="normal")
        self.add_inventory("Jonathan", name="Sol Ring", sid="owned-foil-art", finish="foil")
        comparison, _ = app_module.deck_comparison(deck_id, "Jonathan", fuzzy=True)
        item = comparison[0]
        self.assertEqual(item["free_available"], 1)
        self.assertEqual([source["finish"] for source in item["sources"]], ["normal"])

    def test_fuzzy_assignment_updates_deck_entry_to_selected_inventory_printing(self) -> None:
        deck_id = self.add_deck("Alt-art deck")
        self.add_requirement(deck_id, name="Sol Ring", quantity=1, sid="archidekt-art-id")
        conn = self.connect()
        cur = conn.execute(
            """INSERT INTO inventory(scryfall_id,name,set_code,rarity,price,owner,quantity,finish,
               collector_number,tcgplayer_id,location,deck_id,for_vending)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,0)""",
            ("actual-alt-art-id", "Sol Ring", "SLD", "Rare", "8.00", "Jonathan", 1,
             "normal", "1234", "55555", "Binder C"),
        )
        inventory_id = int(cur.lastrowid)
        conn.commit()
        conn.close()

        response = self.client.post(
            f"/decks/{deck_id}/move-inventory",
            data={"csrf_token": self.csrf, "inventory_id": str(inventory_id), "quantity": "1", "fuzzy": "1"},
            base_url="http://localhost",
        )
        self.assertEqual(response.status_code, 302)
        conn = self.connect()
        row = conn.execute(
            "SELECT name,scryfall_id,set_code,collector_number,tcgplayer_id FROM deck_cards WHERE deck_id=?",
            (deck_id,),
        ).fetchone()
        conn.close()
        self.assertEqual((row["name"], row["scryfall_id"], row["set_code"], row["collector_number"], row["tcgplayer_id"]),
                         ("Sol Ring", "actual-alt-art-id", "SLD", "1234", "55555"))

    def test_fuzzy_assignment_updates_basic_land_art_but_keeps_generic_matching(self) -> None:
        deck_id = self.add_deck("Generic land deck")
        conn = self.connect()
        conn.execute(
            """INSERT INTO deck_cards(deck_id,name,quantity,scryfall_id,set_code,collector_number,category,finish,any_printing)
               VALUES(?,?,?,?,?,?,?,?,1)""",
            (deck_id, "Plains", 1, "generic-plains-art", "M21", "262", "Mainboard", "normal"),
        )
        cur = conn.execute(
            """INSERT INTO inventory(scryfall_id,name,set_code,rarity,price,owner,quantity,finish,
               collector_number,tcgplayer_id,location,deck_id,for_vending)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,0)""",
            ("actual-plains-art", "Plains", "UNF", "Basic Land", "0.25", "Jonathan", 1,
             "normal", "216", "33333", "Land Box"),
        )
        inventory_id = int(cur.lastrowid)
        conn.commit()
        conn.close()

        response = self.client.post(
            f"/decks/{deck_id}/move-inventory",
            data={"csrf_token": self.csrf, "inventory_id": str(inventory_id), "quantity": "1", "fuzzy": "0"},
            base_url="http://localhost",
        )
        self.assertEqual(response.status_code, 302)
        conn = self.connect()
        row = conn.execute("SELECT scryfall_id,set_code,collector_number,any_printing FROM deck_cards WHERE deck_id=?", (deck_id,)).fetchone()
        conn.close()
        self.assertEqual((row["scryfall_id"], row["set_code"], row["collector_number"], row["any_printing"]),
                         ("actual-plains-art", "UNF", "216", 1))

    def test_deck_owner_migration_is_one_time_and_does_not_change_inventory_owner(self) -> None:
        deck_id = self.add_deck("Legacy deck", owner="Friend")
        inventory_id = self.add_inventory("Friend")
        conn = self.connect()
        # Simulate a database that already ran the erroneous v39.12 migration.
        conn.execute("INSERT OR IGNORE INTO people(name,active) VALUES('People Jonathan',1)")
        conn.execute("INSERT OR IGNORE INTO app_migrations(name,applied_at) VALUES('deck_single_owner_people_jonathan_v1',CURRENT_TIMESTAMP)")
        conn.execute("DELETE FROM app_migrations WHERE name='deck_single_owner_jonathan_v2'")
        conn.commit()
        conn.close()
        app_module.migrate_db()
        conn = self.connect()
        self.assertEqual(conn.execute("SELECT person_name FROM decklists WHERE id=?", (deck_id,)).fetchone()[0], "Jonathan")
        self.assertEqual(conn.execute("SELECT owner FROM inventory WHERE id=?", (inventory_id,)).fetchone()[0], "Friend")
        self.assertEqual(conn.execute("SELECT active FROM people WHERE name='Jonathan'").fetchone()[0], 1)
        legacy_alias = conn.execute("SELECT active FROM people WHERE name='People Jonathan'").fetchone()
        self.assertTrue(legacy_alias is None or legacy_alias[0] == 0)
        conn.execute("UPDATE decklists SET person_name='Friend' WHERE id=?", (deck_id,))
        conn.commit()
        conn.close()
        app_module.migrate_db()
        conn = self.connect()
        self.assertEqual(conn.execute("SELECT person_name FROM decklists WHERE id=?", (deck_id,)).fetchone()[0], "Friend")
        conn.close()


if __name__ == "__main__":
    unittest.main()
