"""HTTP and persistence tests for PostgreSQL admin manual-add support."""

from __future__ import annotations

import os
import unittest
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from postgres_app_db import connect_app_database
from postgres_db import close_pool, open_pool, run_migrations


@unittest.skipUnless(
    os.environ.get("CARD_SCANNER_POSTGRES_DSN")
    and os.environ.get("CARD_SCANNER_DATABASE_BACKEND") == "postgres",
    "Run with the PostgreSQL backend and development DSN configured",
)
class PostgreSQLManualAddTests(unittest.TestCase):
    RESOLVED_SCRYFALL_ID = "0000579f-7b35-4ed3-b44c-db2a538066fe"

    @classmethod
    def setUpClass(cls):
        open_pool()
        run_migrations()

        import app as app_module

        if app_module.DATABASE_BACKEND != "postgres":
            raise RuntimeError("Manual-add tests require PostgreSQL app mode")

        cls.app_module = app_module
        cls.app_module.app.config["TESTING"] = True

    @classmethod
    def tearDownClass(cls):
        close_pool()

    def setUp(self):
        self.token = uuid4().hex
        self.admin_username = f"pg-manual-admin-{self.token}"
        self.user_username = f"pg-manual-user-{self.token}"
        self.owner_name = f"PG Manual Owner {self.token}"
        self.user_owner_name = f"PG Manual User Owner {self.token}"
        self.csrf = f"csrf-manual-{self.token}"
        self.inventory_name = f"PG Manual Test Card {self.token}"
        self.inventory_ids = []

        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            person = conn.execute(
                """
                INSERT INTO app.people (name, active)
                VALUES (?, TRUE)
                RETURNING id
                """,
                (self.owner_name,),
            ).fetchone()
            self.person_id = int(person["id"])
            user_person = conn.execute(
                """
                INSERT INTO app.people (name, active)
                VALUES (?, TRUE)
                RETURNING id
                """,
                (self.user_owner_name,),
            ).fetchone()
            self.user_person_id = int(user_person["id"])

            conn.execute(
                """
                INSERT INTO app.users (
                    username, password_hash, is_admin, role,
                    is_superadmin, active, person_id
                )
                VALUES (?, ?, TRUE, 'admin', FALSE, TRUE, ?)
                """,
                (self.admin_username, "unused-manual-test-password-hash", self.person_id),
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
                    self.user_username,
                    "unused-manual-test-password-hash",
                    self.user_person_id,
                    self.admin_username,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        self.admin_client = self.app_module.app.test_client()
        self.user_client = self.app_module.app.test_client()

        with self.admin_client.session_transaction() as session:
            session["username"] = self.admin_username
            session["csrf_token"] = self.csrf

        with self.user_client.session_transaction() as session:
            session["username"] = self.user_username
            session["csrf_token"] = self.csrf

    def tearDown(self):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            # This owner name is unique to the test. Remove only its fixture
            # rows, then its test users and person.
            conn.execute(
                "DELETE FROM app.inventory WHERE owner = ?",
                (self.owner_name,),
            )
            conn.execute(
                "DELETE FROM app.users WHERE username IN (?, ?)",
                (self.admin_username, self.user_username),
            )
            conn.execute(
                "DELETE FROM app.people WHERE id IN (?, ?)",
                (self.person_id, self.user_person_id),
            )
            conn.commit()
        finally:
            conn.close()

    def post_manual_add(self, **overrides):
        data = {
            "name": self.inventory_name,
            "owner": self.owner_name,
            "finish": "normal",
            "quantity": "1",
            "scryfall_id": f"pg-manual-{self.token}",
            "set_code": "ZZT",
            "collector_number": "TEST-1",
            "rarity": "Rare",
            "tcgplayer_id": "",
            "price": "0.00",
            "location": "Free",
            "csrf_token": self.csrf,
        }
        data.update(overrides)
        return self.admin_client.post(
            "/admin/manual_add",
            data=data,
            base_url="http://localhost",
        )

    def insert_inventory(
        self,
        scryfall_id,
        *,
        quantity=1,
        for_vending=False,
        finish="normal",
        location="Free",
    ):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            row = conn.execute(
                """
                INSERT INTO app.inventory (
                    game_id, scryfall_id, name, set_code, rarity, price,
                    quantity, owner, person_id, finish, collector_number,
                    location, for_vending
                )
                VALUES (
                    'mtg', ?, ?, 'OLD', 'Rare', '1.00',
                    ?, ?, ?, ?, 'OLD-1', ?, ?
                )
                RETURNING id
                """,
                (
                    scryfall_id,
                    self.inventory_name,
                    quantity,
                    self.owner_name,
                    self.person_id,
                    finish,
                    location,
                    for_vending,
                ),
            ).fetchone()
            conn.commit()
            return int(row["id"])
        finally:
            conn.close()

    def fetch_inventory(self, scryfall_id):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            return conn.execute(
                """
                SELECT
                    id, card_id, printing_id, scryfall_id, name,
                    set_code, rarity, price, quantity, owner, person_id,
                    finish, collector_number, tcgplayer_id, location,
                    deck_id, for_vending, scan_time
                FROM app.inventory
                WHERE game_id = 'mtg'
                  AND owner = ?
                  AND scryfall_id = ?
                ORDER BY id
                """,
                (self.owner_name, scryfall_id),
            ).fetchall()
        finally:
            conn.close()

    def test_admin_page_shows_supported_panel_and_hides_unconverted_panels(self):
        response = self.admin_client.get(
            "/admin",
            base_url="http://localhost",
        )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        html = response.get_data(as_text=True)

        self.assertNotIn("Add Card Manually", html)
        self.assertNotIn("manualAddForm", html)
        self.assertIn("Catalog search aliases", html)
        self.assertIn("catalogSearch", html)
        self.assertIn("aliasForm", html)
        self.assertIn(self.owner_name, html)
        self.assertIn("Log out", html)

        self.assertIn("accountWorkspaceTitle", html)
        self.assertNotIn("Price History &amp; Discord Alerts", html)
        self.assertIn("Web accounts</h3>", html)
        self.assertNotIn("Back to inventory", html)
        self.assertIn(">Decks</a>", html)
        self.assertIn(">Vending</a>", html)

    def test_new_manual_card_links_known_catalog_printing(self):
        sid = self.RESOLVED_SCRYFALL_ID

        response = self.post_manual_add(
            scryfall_id=sid,
            name=self.inventory_name,
            set_code="TST",
            collector_number="123",
            tcgplayer_id="98765",
            price="0.35",
            quantity="2",
        )

        self.assertEqual(response.status_code, 302)
        rows = self.fetch_inventory(sid)
        self.assertEqual(len(rows), 1)

        row = rows[0]
        self.assertIsNotNone(row["card_id"])
        self.assertIsNotNone(row["printing_id"])
        self.assertEqual(row["person_id"], self.person_id)
        self.assertEqual(row["quantity"], 2)
        self.assertEqual(row["owner"], self.owner_name)
        self.assertEqual(row["set_code"], "TST")
        self.assertEqual(row["collector_number"], "123")
        self.assertEqual(row["tcgplayer_id"], "98765")
        self.assertEqual(row["price"], "0.35")
        self.assertFalse(row["for_vending"])

    def test_matching_free_row_merges_and_updates_metadata(self):
        sid = f"pg-manual-merge-{self.token}"
        existing_id = self.insert_inventory(sid, quantity=2)

        response = self.post_manual_add(
            scryfall_id=sid,
            quantity="3",
            price="0.25",
            set_code="NEW",
            rarity="Mythic",
            collector_number="22",
            tcgplayer_id="12345",
        )

        self.assertEqual(response.status_code, 302)
        rows = self.fetch_inventory(sid)
        self.assertEqual(len(rows), 1)

        row = rows[0]
        self.assertEqual(row["id"], existing_id)
        self.assertEqual(row["quantity"], 5)
        self.assertEqual(row["price"], "0.25")
        self.assertEqual(row["set_code"], "NEW")
        self.assertEqual(row["rarity"], "Mythic")
        self.assertEqual(row["collector_number"], "22")
        self.assertEqual(row["tcgplayer_id"], "12345")

    def test_manual_add_never_merges_into_vending_stock(self):
        sid = f"pg-manual-vending-{self.token}"
        vending_id = self.insert_inventory(
            sid,
            quantity=7,
            for_vending=True,
        )

        response = self.post_manual_add(
            scryfall_id=sid,
            quantity="2",
        )

        self.assertEqual(response.status_code, 302)
        rows = self.fetch_inventory(sid)
        self.assertEqual(len(rows), 2)

        vending_rows = [row for row in rows if row["for_vending"]]
        normal_rows = [row for row in rows if not row["for_vending"]]

        self.assertEqual(len(vending_rows), 1)
        self.assertEqual(vending_rows[0]["id"], vending_id)
        self.assertEqual(vending_rows[0]["quantity"], 7)

        self.assertEqual(len(normal_rows), 1)
        self.assertEqual(normal_rows[0]["quantity"], 2)

    def test_different_finish_does_not_merge(self):
        sid = f"pg-manual-finish-{self.token}"
        self.insert_inventory(sid, quantity=4, finish="foil")

        response = self.post_manual_add(
            scryfall_id=sid,
            quantity="2",
            finish="normal",
        )

        self.assertEqual(response.status_code, 302)
        rows = self.fetch_inventory(sid)
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            sorted((row["finish"], row["quantity"]) for row in rows),
            [("foil", 4), ("normal", 2)],
        )

    def test_inactive_or_unknown_owner_is_rejected(self):
        sid = f"pg-manual-invalid-owner-{self.token}"

        response = self.post_manual_add(
            scryfall_id=sid,
            owner=f"Unknown owner {self.token}",
        )

        self.assertEqual(response.status_code, 302)
        message = parse_qs(urlparse(response.location).query).get("message", [""])[0]
        self.assertEqual(message, "Choose an active owner.")
        self.assertEqual(self.fetch_inventory(sid), [])

    def test_manual_add_requires_csrf(self):
        response = self.admin_client.post(
            "/admin/manual_add",
            data={
                "name": self.inventory_name,
                "owner": self.owner_name,
                "quantity": "1",
            },
            base_url="http://localhost",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("CSRF", response.get_data(as_text=True))

    def test_user_cannot_open_admin_or_add_to_another_account_collection(self):
        page = self.user_client.get(
            "/admin",
            base_url="http://localhost",
        )
        self.assertEqual(page.status_code, 403)

        blocked = self.user_client.post(
            "/admin/manual_add",
            data={
                "name": self.inventory_name,
                "owner": self.owner_name,
                "quantity": "1",
                "csrf_token": self.csrf,
            },
            base_url="http://localhost",
        )
        self.assertEqual(blocked.status_code, 302)
        self.assertIn("/?", blocked.location)
        self.assertEqual(self.fetch_inventory(f"pg-manual-{self.token}"), [])

        allowed = self.user_client.post(
            "/admin/manual_add",
            data={
                "name": self.inventory_name,
                "owner": self.user_owner_name,
                "quantity": "1",
                "scryfall_id": f"pg-manual-user-{self.token}",
                "csrf_token": self.csrf,
            },
            base_url="http://localhost",
        )
        self.assertEqual(allowed.status_code, 302)
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            row = conn.execute(
                "SELECT owner, person_id, quantity FROM app.inventory WHERE owner=? AND scryfall_id=?",
                (self.user_owner_name, f"pg-manual-user-{self.token}"),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["person_id"], self.user_person_id)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
