"""Regression tests for independent storage, deck assignment, and vending flags.

All tests use temporary SQLite databases. They never open the user's live database
or access Scryfall/TCGCSV.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

_IMPORT_DATA_DIR = tempfile.mkdtemp(prefix="cardscanner-inventory-test-import-")
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


class InventoryManagementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="cardscanner-inventory-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        app_module.DB_INV = self.root / "mtg_inventory.db"
        app_module.DB_ART = self.root / "missing-art.db"
        app_module.PRICES_FILE = self.root / "tcg_prices.json"
        app_module.PRICE_META_FILE = self.root / "price_sync.json"
        app_module._PRICE_CACHE = {"key": None, "prices": {}}
        app_module._AUTH_READY = False
        app_module.migrate_db()
        self.client = app_module.app.test_client()
        self.csrf = "unit-test-csrf-token-012345678901234567890"
        with self.client.session_transaction() as session:
            session["username"] = "ci-admin"
            session["csrf_token"] = self.csrf

    def connect(self) -> sqlite3.Connection:
        return app_module.connect_inventory()

    def add_card(
        self,
        name: str,
        *,
        owner: str = "Me",
        location: str = "Free",
        set_code: str = "tst",
        collector_number: str = "1",
        scryfall_id: str | None = None,
        finish: str = "normal",
        quantity: int = 1,
        deck_id: int | None = None,
        for_vending: int = 0,
        rarity: str = "Rare",
    ) -> int:
        conn = self.connect()
        cur = conn.execute(
            """INSERT INTO inventory
               (scryfall_id,name,set_code,rarity,price,scan_time,quantity,owner,finish,
                collector_number,tcgplayer_id,location,deck_id,for_vending)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (scryfall_id or f"sid-{name.replace(' ', '-').lower()}", name, set_code,
             rarity, "1.00", datetime.now(timezone.utc).isoformat(), quantity, owner,
             finish, collector_number, None, location, deck_id, for_vending),
        )
        item_id = int(cur.lastrowid)
        conn.commit()
        conn.close()
        return item_id

    def add_deck(self, name: str = "Deck Alpha") -> int:
        conn = self.connect()
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "INSERT INTO decklists(name,source,source_url,created_at,updated_at) VALUES(?,?,?,?,?)",
            (name, "manual", None, now, now),
        )
        deck_id = int(cur.lastrowid)
        conn.commit()
        conn.close()
        return deck_id

    def post_json(self, path: str, payload: dict):
        return self.client.post(
            path,
            json=payload,
            headers={"X-CSRFToken": self.csrf},
            base_url="http://localhost",
        )

    def test_legacy_migration_marks_free_pool_for_vending_and_preserves_deck_links(self) -> None:
        legacy_db = self.root / "legacy.db"
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(legacy_db) as conn:
            conn.execute(
                """CREATE TABLE inventory (
                    id INTEGER PRIMARY KEY, scryfall_id TEXT, name TEXT NOT NULL,
                    set_code TEXT, rarity TEXT, price TEXT DEFAULT '0.00',
                    scan_time TEXT DEFAULT CURRENT_TIMESTAMP, quantity INTEGER DEFAULT 1,
                    owner TEXT DEFAULT 'Me', finish TEXT DEFAULT 'normal',
                    collector_number TEXT, tcgplayer_id TEXT, location TEXT DEFAULT 'Free'
                )"""
            )
            conn.execute(
                """CREATE TABLE decklists (
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'manual',
                    source_url TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                "INSERT INTO decklists(id,name,source,created_at,updated_at) VALUES(1,?,?,?,?)",
                ("Legacy Deck", "manual", now, now),
            )
            conn.executemany(
                "INSERT INTO inventory(id,name,owner,location,quantity,finish) VALUES(?,?,?,?,?,?)",
                [(1, "Free Pool Card", "Friend", "Free", 2, "normal"),
                 (2, "Legacy Assigned Card", "Friend", "Legacy Deck", 1, "normal")],
            )
        original_db = app_module.DB_INV
        original_auth = app_module._AUTH_READY
        try:
            app_module.DB_INV = legacy_db
            app_module._AUTH_READY = False
            app_module.migrate_db()
            with sqlite3.connect(legacy_db) as conn:
                rows = conn.execute(
                    "SELECT id,deck_id,for_vending FROM inventory ORDER BY id"
                ).fetchall()
                self.assertEqual(rows[0][1], None)
            self.assertEqual(rows[0][2], 1, "legacy Free-pool row should default to vending")
            self.assertEqual(rows[1][1], 1, "legacy location matching a deck name should migrate once")
            self.assertEqual(rows[1][2], 0, "deck-assigned rows should not be sellable")
            # A second migration must not undo an administrator opting out of vending.
            with sqlite3.connect(legacy_db) as conn:
                conn.execute("UPDATE inventory SET for_vending=0 WHERE id=1")
            app_module.migrate_db()
            with sqlite3.connect(legacy_db) as conn:
                self.assertEqual(conn.execute("SELECT for_vending FROM inventory WHERE id=1").fetchone()[0], 0)
        finally:
            app_module.DB_INV = original_db
            app_module._AUTH_READY = original_auth

    def test_new_cards_in_free_are_not_automatically_marked_for_vending(self) -> None:
        response = self.client.post(
            "/admin/manual_add",
            data={"name": "New Free Pool Card", "owner": "Me", "location": "Free", "quantity": "1", "csrf_token": self.csrf},
            base_url="http://localhost",
        )
        self.assertEqual(response.status_code, 302)
        conn = self.connect()
        row = conn.execute("SELECT location,for_vending FROM inventory WHERE name='New Free Pool Card'").fetchone()
        conn.close()
        self.assertEqual(row["location"], "Free")
        self.assertEqual(row["for_vending"], 0, "new Free-pool rows should require an explicit vending opt-in")

    def test_scanner_does_not_merge_new_pool_copies_into_vending_stock(self) -> None:
        self.add_card("Scanned Printing", owner="Me", location="Free", scryfall_id="sid-scanned", quantity=3, for_vending=1)
        result = app_module.insert_or_increment_scan({
            "scryfall_id": "sid-scanned", "name": "Scanned Printing", "owner": "Me",
            "location": "Free", "quantity": 1, "finish": "normal", "tcgplayer_id": "987654",
        })
        self.assertTrue(result["success"])
        conn = self.connect()
        rows = conn.execute(
            "SELECT quantity,for_vending FROM inventory WHERE scryfall_id='sid-scanned' ORDER BY for_vending"
        ).fetchall()
        conn.close()
        self.assertEqual([(row["quantity"], row["for_vending"]) for row in rows], [(1, 0), (3, 1)])

    def test_manual_add_does_not_merge_new_pool_copies_into_vending_stock(self) -> None:
        self.add_card("Same Printing", owner="Me", location="Free", scryfall_id="sid-same", quantity=2, for_vending=1)
        response = self.client.post(
            "/admin/manual_add",
            data={"name": "Same Printing", "owner": "Me", "location": "Free", "quantity": "1", "scryfall_id": "sid-same", "set_code": "tst", "csrf_token": self.csrf},
            base_url="http://localhost",
        )
        self.assertEqual(response.status_code, 302)
        conn = self.connect()
        rows = conn.execute(
            "SELECT quantity,for_vending FROM inventory WHERE scryfall_id='sid-same' ORDER BY for_vending"
        ).fetchall()
        conn.close()
        self.assertEqual([(row["quantity"], row["for_vending"]) for row in rows], [(1, 0), (2, 1)])

    def test_bulk_marks_friend_cards_in_multiple_locations_for_vending(self) -> None:
        ids = [
            self.add_card("Friend Binder Card", owner="Nick", location="Binder 2", for_vending=0),
            self.add_card("Friend Box Card", owner="Nick", location="Storage Box 4", for_vending=0),
        ]
        response = self.post_json("/admin/inventory/bulk-update", {"ids": ids, "for_vending": "1"})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json()["updated"], 2)
        conn = self.connect()
        rows = conn.execute("SELECT location,for_vending FROM inventory WHERE id IN (?,?) ORDER BY id", ids).fetchall()
        conn.close()
        self.assertEqual([(r["location"], r["for_vending"]) for r in rows], [("Binder 2", 1), ("Storage Box 4", 1)])
        vending_rows, *_ = app_module.inventory_rows("", "", "", "date", "desc", "", "", "", "yes")
        vending_ids = {int(row["id"]) for row in vending_rows}
        self.assertTrue(set(ids).issubset(vending_ids), "vending must include explicitly enabled cards outside Free")

    def test_bulk_rejects_vending_for_deck_assigned_cards(self) -> None:
        deck_id = self.add_deck()
        card_id = self.add_card("Deck Card", location="Binder 3", deck_id=deck_id)
        response = self.post_json("/admin/inventory/bulk-update", {"ids": [card_id], "for_vending": "1"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("assigned to decks", response.get_json()["error"])

    def test_bulk_can_unassign_and_mark_vending_in_one_operation(self) -> None:
        deck_id = self.add_deck()
        card_id = self.add_card("Unassign to Sell", location="Binder 1", deck_id=deck_id)
        response = self.post_json(
            "/admin/inventory/bulk-update",
            {"ids": [card_id], "deck_id": "none", "for_vending": "1"},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        conn = self.connect()
        row = conn.execute("SELECT deck_id,location,for_vending FROM inventory WHERE id=?", (card_id,)).fetchone()
        conn.close()
        self.assertIsNone(row["deck_id"])
        self.assertEqual(row["location"], "Binder 1")
        self.assertEqual(row["for_vending"], 1)

    def test_owner_transfer_does_not_merge_rows_with_different_vending_flags(self) -> None:
        conn = self.connect()
        conn.execute("INSERT OR IGNORE INTO people(name,active) VALUES('Nick',1)")
        conn.commit()
        conn.close()
        self.add_card("Shared Printing", owner="Nick", location="Binder 2", scryfall_id="sid-shared", quantity=2, for_vending=1)
        self.add_card("Shared Printing", owner="Me", location="Binder 2", scryfall_id="sid-shared", quantity=1, for_vending=0)
        conn = self.connect()
        nick = conn.execute("SELECT id FROM people WHERE name='Nick'").fetchone()
        conn.close()
        response = self.client.post(
            f"/admin/people/{nick['id']}/delete",
            data={"transfer_to": "Me", "csrf_token": self.csrf},
            base_url="http://localhost",
        )
        self.assertEqual(response.status_code, 302)
        conn = self.connect()
        rows = conn.execute(
            "SELECT quantity,for_vending FROM inventory WHERE owner='Me' AND scryfall_id='sid-shared' ORDER BY for_vending"
        ).fetchall()
        conn.close()
        self.assertEqual([(row["quantity"], row["for_vending"]) for row in rows], [(1, 0), (2, 1)])

    def test_location_edit_does_not_assign_deck_by_name(self) -> None:
        self.add_deck("Deck Alpha")
        card_id = self.add_card("Storage Card", location="Binder 1")
        response = self.post_json("/update", {"id": card_id, "field": "location", "value": "Deck Alpha"})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        conn = self.connect()
        row = conn.execute("SELECT location,deck_id FROM inventory WHERE id=?", (card_id,)).fetchone()
        conn.close()
        self.assertEqual(row["location"], "Deck Alpha")
        self.assertIsNone(row["deck_id"], "a storage label that resembles a deck must not assign membership")

    def test_deck_comparison_uses_deck_id_and_not_storage_location(self) -> None:
        deck_id = self.add_deck()
        conn = self.connect()
        conn.execute(
            "INSERT INTO deck_cards(deck_id,name,quantity,scryfall_id,set_code,collector_number,category,finish) VALUES(?,?,?,?,?,?,?,?)",
            (deck_id, "Deck Card", 3, "sid-deck-card", "tst", "1", "Mainboard", "normal"),
        )
        conn.commit()
        conn.close()
        self.add_card("Deck Card", owner="Friend", location="Deck Alpha", scryfall_id="sid-deck-card", deck_id=deck_id, quantity=1, for_vending=0)
        self.add_card("Deck Card", owner="Friend", location="Long-term Storage", scryfall_id="sid-deck-card", quantity=1, for_vending=0)
        self.add_card("Deck Card", owner="Friend", location="Binder 9", scryfall_id="sid-deck-card", quantity=1, for_vending=0)
        comparison, stats = app_module.deck_comparison(deck_id, "Friend")
        self.assertEqual(comparison[0]["assigned"], 1)
        self.assertEqual(comparison[0]["free_available"], 2)
        self.assertEqual(comparison[0]["missing"], 0)
        self.assertEqual(stats["owned"], 3)

    def test_scryfall_style_filter_and_numeric_set_collector_sort(self) -> None:
        # Two cards prove numeric collector ordering within the same set.
        id10 = self.add_card("Red Green Creature 10", set_code="mh3", collector_number="10", scryfall_id="sid-mh3-10")
        id2 = self.add_card("Red Green Creature 2", set_code="mh3", collector_number="2", scryfall_id="sid-mh3-2")
        conn = self.connect()
        for sid, name, number in [
            ("sid-mh3-10", "Red Green Creature 10", 10),
            ("sid-mh3-2", "Red Green Creature 2", 2),
        ]:
            conn.execute(
                """INSERT INTO card_metadata
                   (scryfall_id,name,type_line,colors_json,colors_text,color_identity_json,mana_value,mana_cost,oracle_text,oracle_id,legalities_json,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sid, name, "Creature", '["R","G"]', "RG", '["R","G"]', 2, "{R}{G}", "Draw a card.", None, "{}", datetime.now(timezone.utc).isoformat()),
            )
        conn.commit()
        conn.close()
        rows, *_ = app_module.inventory_rows(
            'set:mh3 t:creature c:rg o:"draw a card" r>=rare order:set direction:asc',
            "", "", "date", "desc",
        )
        self.assertEqual([row["id"] for row in rows], [id2, id10])

    def test_bulk_update_can_change_owner_for_selected_rows(self) -> None:
        conn = self.connect()
        conn.execute("INSERT OR IGNORE INTO people(name,active) VALUES('Jonathan',1)")
        conn.commit()
        conn.close()
        ids = [
            self.add_card("Bulk Owner A", owner="Me", location="Binder 1", for_vending=1),
            self.add_card("Bulk Owner B", owner="Me", location="Storage Box", for_vending=1),
        ]
        response = self.post_json("/admin/inventory/bulk-update", {"ids": ids, "owner": "Jonathan"})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        conn = self.connect()
        rows = conn.execute("SELECT owner,location,for_vending FROM inventory WHERE id IN (?,?) ORDER BY id", ids).fetchall()
        conn.close()
        self.assertEqual([(r["owner"], r["location"], r["for_vending"]) for r in rows], [
            ("Jonathan", "Binder 1", 1), ("Jonathan", "Storage Box", 1),
        ])

    def test_superadmin_can_link_account_to_person_and_set_default_collection(self) -> None:
        conn = self.connect()
        conn.execute("INSERT OR IGNORE INTO people(name,active) VALUES('Jonathan',1)")
        conn.execute("INSERT OR IGNORE INTO people(name,active) VALUES('Nick',1)")
        person_id = int(conn.execute("SELECT id FROM people WHERE name='Jonathan'").fetchone()[0])
        conn.execute(
            "INSERT INTO users(username,password_hash,is_admin,role,is_superadmin,active) VALUES(?,?,?,?,?,1)",
            ("jonathan-login", app_module.generate_password_hash("linked-user-password-123"), 1, "admin", 0),
        )
        conn.commit()
        conn.close()
        self.add_card("Jonathan Collection Card", owner="Jonathan")
        self.add_card("Nick Collection Card", owner="Nick")
        self.add_card("Jonathan Vending Card", owner="Jonathan", for_vending=1)
        self.add_card("Nick Vending Card", owner="Nick", for_vending=1)

        response = self.client.post(
            "/admin/users/jonathan-login/person",
            data={"person_id": str(person_id), "csrf_token": self.csrf},
            base_url="http://localhost",
        )
        self.assertEqual(response.status_code, 302)
        conn = self.connect()
        linked = conn.execute("SELECT person_id FROM users WHERE username='jonathan-login'").fetchone()
        conn.close()
        self.assertEqual(linked["person_id"], person_id)

        # On the next request, this account defaults to its mapped Person rather than another owner's inventory.
        with self.client.session_transaction() as session:
            session.clear()
            session["username"] = "jonathan-login"
            session["csrf_token"] = "linked-account-csrf-token-0123456789"
        inventory_response = self.client.get("/", base_url="http://localhost")
        self.assertEqual(inventory_response.status_code, 200)
        page = inventory_response.get_data(as_text=True)
        self.assertIn("Jonathan Collection Card", page)
        self.assertNotIn("Nick Collection Card", page)

        vending_response = self.client.get("/vending", base_url="http://localhost")
        self.assertEqual(vending_response.status_code, 200)
        vending_page = vending_response.get_data(as_text=True)
        self.assertIn("Jonathan Vending Card", vending_page)
        self.assertIn("Nick Vending Card", vending_page, "vending is shared across owners by default")

        deck_id = self.add_deck("Jonathan Deck")
        deck_response = self.client.get(f"/decks/{deck_id}", base_url="http://localhost")
        self.assertEqual(deck_response.status_code, 200)
        deck_page = deck_response.get_data(as_text=True)
        self.assertIn('<option value="Jonathan" selected>', deck_page)

    def test_person_assignment_form_in_admin_has_csrf_token(self) -> None:
        conn = self.connect()
        conn.execute("INSERT INTO users(username,password_hash,is_admin,role,is_superadmin,active) VALUES(?,?,?,?,?,1)",
                     ("needs-person-link", app_module.generate_password_hash("user-password-long-enough"), 1, "admin", 0))
        conn.commit()
        conn.close()
        response = self.client.get("/admin", base_url="http://localhost")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        action = 'action="/admin/users/needs-person-link/person"'
        self.assertIn(action, page)
        form_start = page.index(action)
        form_end = page.index("</form>", form_start)
        self.assertIn('name="csrf_token"', page[form_start:form_end])
        create_action = 'action="/admin/users"'
        self.assertIn(create_action, page)
        create_start = page.index(create_action)
        create_end = page.index("</form>", create_start)
        self.assertIn('name="csrf_token"', page[create_start:create_end])

    def test_vending_page_uses_shared_selection_and_admin_bulk_controls(self) -> None:
        self.add_card("Selectable Vending Card", owner="Me", for_vending=1)
        response = self.client.get("/vending", base_url="http://localhost")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("multi-select.js", page)
        self.assertIn("vendorApplyBulkEdit", page)
        self.assertIn("vendor-select-checkbox", page)

    def test_legacy_user_schema_migrates_person_assignment_column_without_losing_accounts(self) -> None:
        legacy_db = self.root / "legacy-users.db"
        with sqlite3.connect(legacy_db) as conn:
            conn.execute("""CREATE TABLE users (
                username TEXT PRIMARY KEY COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            conn.execute(
                "INSERT INTO users(username,password_hash,is_admin) VALUES(?,?,?)",
                ("legacy-admin", "already-hashed-password", 1),
            )
        original_db = app_module.DB_INV
        original_auth = app_module._AUTH_READY
        try:
            app_module.DB_INV = legacy_db
            app_module._AUTH_READY = False
            app_module.migrate_db()
            with sqlite3.connect(legacy_db) as conn:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
                self.assertIn("person_id", columns)
                row = conn.execute("SELECT username,password_hash,person_id FROM users WHERE username='legacy-admin'").fetchone()
                self.assertEqual(row, ("legacy-admin", "already-hashed-password", None))
        finally:
            app_module.DB_INV = original_db
            app_module._AUTH_READY = original_auth

    def test_inventory_uses_shared_multi_select_script_and_safe_modes(self) -> None:
        self.add_card("UI mode test card")
        response = self.client.get("/?view=grid", base_url="http://localhost")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn('data-inventory-mode="browse"', page)
        self.assertIn('data-mode="select"', page)
        self.assertIn('data-mode="edit"', page)
        self.assertIn('multi-select.js', page)
        self.assertIn('edit-only-control', page)


if __name__ == "__main__":
    unittest.main()
