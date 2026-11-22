"""PostgreSQL HTTP and persistence tests for admin People management."""

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
class PostgreSQLPeopleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        open_pool()
        run_migrations()
        import app as app_module
        if app_module.DATABASE_BACKEND != "postgres":
            raise RuntimeError("People tests require PostgreSQL app mode")
        cls.app_module = app_module
        cls.app_module.app.config["TESTING"] = True

    @classmethod
    def tearDownClass(cls):
        close_pool()

    def setUp(self):
        self.token = uuid4().hex
        self.source_name = f"PG People Source {self.token}"
        self.target_name = f"PG People Target {self.token}"
        self.inactive_name = f"PG People Inactive {self.token}"
        self.admin_username = f"pg-people-admin-{self.token}"
        self.user_username = f"pg-people-user-{self.token}"
        self.csrf = f"csrf-people-{self.token}"
        self.sids = []
        self.person_ids = []
        self.deck_ids = []

        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            for name, active in (
                (self.source_name, True),
                (self.target_name, True),
                (self.inactive_name, False),
            ):
                row = conn.execute(
                    """
                    INSERT INTO app.people (name, active)
                    VALUES (?, ?)
                    RETURNING id
                    """,
                    (name, active),
                ).fetchone()
                self.person_ids.append(int(row["id"]))

            self.source_id, self.target_id, self.inactive_id = self.person_ids
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
                    "unused-people-test-password-hash",
                    self.source_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO app.users (
                    username, password_hash, is_admin, role,
                    is_superadmin, active, manager_username, person_id
                )
                VALUES (?, ?, FALSE, 'user', FALSE, TRUE, ?, ?)
                """,
                (
                    self.user_username,
                    "unused-people-test-password-hash",
                    self.admin_username,
                    self.target_id,
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
            for sid in self.sids:
                conn.execute(
                    "DELETE FROM app.inventory WHERE game_id = 'mtg' AND scryfall_id = ?",
                    (sid,),
                )
            for deck_id in self.deck_ids:
                conn.execute("DELETE FROM app.decklists WHERE id = ?", (deck_id,))
            conn.execute(
                "DELETE FROM app.users WHERE username IN (?, ?)",
                (self.admin_username, self.user_username),
            )
            for person_id in self.person_ids:
                conn.execute("DELETE FROM app.people WHERE id = ?", (person_id,))
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def message(response):
        return parse_qs(urlparse(response.location).query).get("message", [""])[0]

    def post(self, path, data=None, *, csrf=True, client=None):
        payload = dict(data or {})
        if csrf:
            payload["csrf_token"] = self.csrf
        return (client or self.admin_client).post(
            path,
            data=payload,
            base_url="http://localhost",
        )

    def insert_inventory(
        self,
        sid,
        *,
        owner="source",
        quantity=1,
        finish="normal",
        location="Free",
        deck_id=None,
        for_vending=False,
    ):
        person_id = self.source_id if owner == "source" else self.target_id
        owner_name = self.source_name if owner == "source" else self.target_name
        self.sids.append(sid)
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            row = conn.execute(
                """
                INSERT INTO app.inventory (
                    game_id, scryfall_id, name, quantity,
                    owner, person_id, finish, location, deck_id, for_vending
                )
                VALUES ('mtg', ?, 'People transfer fixture', ?, ?, ?, ?, ?, ?, ?)
                RETURNING id
                """,
                (
                    sid, quantity, owner_name, person_id,
                    finish, location, deck_id, for_vending,
                ),
            ).fetchone()
            conn.commit()
            return int(row["id"])
        finally:
            conn.close()

    def rows_for_sid(self, sid):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            return conn.execute(
                """
                SELECT id, quantity, owner, person_id, finish,
                       location, deck_id, for_vending
                FROM app.inventory
                WHERE game_id = 'mtg' AND scryfall_id = ?
                ORDER BY id
                """,
                (sid,),
            ).fetchall()
        finally:
            conn.close()

    def make_source_collection_unassigned(self):
        """Move the test admin to a fresh collection so source_id is unassigned."""
        reserve_name = f"PG People Admin Reserve {self.token}"
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            reserve = conn.execute(
                """
                INSERT INTO app.people (name, active)
                VALUES (?, TRUE)
                RETURNING id
                """,
                (reserve_name,),
            ).fetchone()
            reserve_id = int(reserve["id"])
            self.person_ids.append(reserve_id)
            conn.execute(
                """
                UPDATE app.users
                SET person_id = ?, is_admin = TRUE, is_superadmin = TRUE,
                    role = 'superadmin'
                WHERE username = ?
                """,
                (reserve_id, self.admin_username),
            )
            conn.commit()
            return reserve_id
        finally:
            conn.close()

    def create_deck(self):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            row = conn.execute(
                """
                INSERT INTO app.decklists (
                    game_id, name, source, created_at, updated_at,
                    person_id, person_name
                )
                VALUES ('mtg', ?, 'manual', now(), now(), ?, ?)
                RETURNING id
                """,
                (
                    f"People transfer deck {self.token}",
                    self.source_id,
                    self.source_name,
                ),
            ).fetchone()
            conn.commit()
            deck_id = int(row["id"])
            self.deck_ids.append(deck_id)
            return deck_id
        finally:
            conn.close()

    def test_admin_page_exposes_people_but_keeps_other_panels_hidden(self):
        response = self.admin_client.get("/admin", base_url="http://localhost")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        html = response.get_data(as_text=True)
        self.assertIn("Collection owners</h3>", html)
        self.assertIn('action="/admin/people"', html)
        self.assertIn("Delete permanently", html)
        self.assertNotIn("Price History &amp; Discord Alerts", html)
        self.assertIn("Web accounts</h3>", html)
        self.assertNotIn("Admin password</h2>", html)

    def test_add_person_creates_active_person(self):
        name = f"PG People New {self.token}"
        response = self.post("/admin/people", {"name": name})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.message(response), f"Added {name}")

        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            row = conn.execute(
                "SELECT id, active FROM app.people WHERE name = ?",
                (name,),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertTrue(row["active"])
            self.person_ids.append(int(row["id"]))
        finally:
            conn.close()

    def test_add_person_reactivates_case_insensitive_existing_name(self):
        response = self.post(
            "/admin/people",
            {"name": self.inactive_name.lower()},
        )
        self.assertEqual(response.status_code, 302)

        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            row = conn.execute(
                "SELECT id, name, active FROM app.people WHERE id = ?",
                (self.inactive_id,),
            ).fetchone()
            self.assertEqual(int(row["id"]), self.inactive_id)
            self.assertEqual(row["name"], self.inactive_name)
            self.assertTrue(row["active"])
        finally:
            conn.close()

    def test_toggle_person_flips_active_state(self):
        self.assertEqual(
            self.post(f"/admin/people/{self.source_id}/toggle").status_code,
            302,
        )
        self.assertEqual(
            self.post(f"/admin/people/{self.source_id}/toggle").status_code,
            302,
        )

        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            active = conn.execute(
                "SELECT active FROM app.people WHERE id = ?",
                (self.source_id,),
            ).fetchone()["active"]
            self.assertTrue(active)
        finally:
            conn.close()

    def test_rename_person_updates_inventory_owner_and_keeps_person_link(self):
        sid = f"pg-people-rename-{self.token}"
        row_id = self.insert_inventory(sid)
        new_name = f"PG People Renamed {self.token}"

        response = self.post(
            f"/admin/people/{self.source_id}/rename",
            {"name": new_name},
        )
        self.assertEqual(response.status_code, 302)
        row = self.rows_for_sid(sid)[0]
        self.assertEqual(row["id"], row_id)
        self.assertEqual(row["owner"], new_name)
        self.assertEqual(row["person_id"], self.source_id)

        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            user = conn.execute(
                "SELECT person_id FROM app.users WHERE username = ?",
                (self.admin_username,),
            ).fetchone()
            self.assertEqual(user["person_id"], self.source_id)
        finally:
            conn.close()

    def test_rename_to_duplicate_name_is_ignored(self):
        response = self.post(
            f"/admin/people/{self.source_id}/rename",
            {"name": self.target_name},
        )
        self.assertEqual(response.status_code, 302)

        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            row = conn.execute(
                "SELECT name FROM app.people WHERE id = ?",
                (self.source_id,),
            ).fetchone()
            self.assertEqual(row["name"], self.source_name)
        finally:
            conn.close()

    def test_delete_assigned_collection_is_rejected(self):
        response = self.post(f"/admin/people/{self.source_id}/delete")
        self.assertEqual(response.status_code, 302)
        self.assertIn(
            f"is the collection owned by {self.admin_username}",
            self.message(response),
        )

        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            person = conn.execute(
                "SELECT id FROM app.people WHERE id = ?",
                (self.source_id,),
            ).fetchone()
            user = conn.execute(
                "SELECT person_id FROM app.users WHERE username = ?",
                (self.admin_username,),
            ).fetchone()
            self.assertIsNotNone(person)
            self.assertEqual(user["person_id"], self.source_id)
        finally:
            conn.close()

    def test_delete_with_inventory_requires_another_active_owner(self):
        self.make_source_collection_unassigned()
        sid = f"pg-people-require-transfer-{self.token}"
        self.insert_inventory(sid, quantity=3)

        missing = self.post(f"/admin/people/{self.source_id}/delete")
        self.assertEqual(missing.status_code, 302)
        self.assertIn("still owns 3 card(s)", self.message(missing))

        inactive = self.post(
            f"/admin/people/{self.source_id}/delete",
            {"transfer_to": self.inactive_name},
        )
        self.assertEqual(inactive.status_code, 302)
        self.assertEqual(
            self.message(inactive),
            "Choose an active owner for the inventory transfer.",
        )

        rows = self.rows_for_sid(sid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["owner"], self.source_name)

    def test_delete_transfer_merges_only_exact_inventory_matches(self):
        reserve_id = self.make_source_collection_unassigned()
        sid = f"pg-people-transfer-{self.token}"
        target_id = self.insert_inventory(
            sid, owner="target", quantity=3, location="Binder",
        )
        self.insert_inventory(sid, quantity=2, location="Binder")
        self.insert_inventory(sid, quantity=1, finish="foil", location="Binder")
        self.insert_inventory(sid, quantity=2, location="Box")
        deck_id = self.create_deck()
        self.insert_inventory(sid, quantity=3, location="Binder", deck_id=deck_id)
        self.insert_inventory(sid, quantity=4, location="Binder", for_vending=True)

        response = self.post(
            f"/admin/people/{self.source_id}/delete",
            {"transfer_to": self.target_name},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("Transferred 12 card(s)", self.message(response))

        rows = self.rows_for_sid(sid)
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(row["owner"] == self.target_name for row in rows))
        self.assertTrue(all(row["person_id"] == self.target_id for row in rows))

        by_id = {int(row["id"]): row for row in rows}
        self.assertEqual(by_id[target_id]["quantity"], 5)
        observed = {
            (
                row["finish"], row["location"], row["deck_id"],
                bool(row["for_vending"]), int(row["quantity"]),
            )
            for row in rows if int(row["id"]) != target_id
        }
        self.assertEqual(
            observed,
            {
                ("foil", "Binder", None, False, 1),
                ("normal", "Binder", deck_id, False, 3),
                ("normal", "Binder", None, True, 4),
                ("normal", "Box", None, False, 2),
            },
        )

        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            source = conn.execute(
                "SELECT id FROM app.people WHERE id = ?",
                (self.source_id,),
            ).fetchone()
            account = conn.execute(
                "SELECT person_id FROM app.users WHERE username = ?",
                (self.admin_username,),
            ).fetchone()
            deck = conn.execute(
                "SELECT person_id, person_name FROM app.decklists WHERE id = ?",
                (deck_id,),
            ).fetchone()
            self.assertIsNone(source)
            self.assertEqual(account["person_id"], reserve_id)
            self.assertIsNone(deck["person_id"])
            self.assertEqual(deck["person_name"], self.source_name)
        finally:
            conn.close()

    def test_all_people_write_routes_require_csrf(self):
        requests = (
            ("/admin/people", {"name": "Missing CSRF"}),
            (f"/admin/people/{self.source_id}/rename", {"name": "Renamed"}),
            (f"/admin/people/{self.source_id}/toggle", {}),
            (f"/admin/people/{self.source_id}/delete", {}),
        )
        for path, payload in requests:
            with self.subTest(path=path):
                response = self.post(path, payload, csrf=False)
                self.assertEqual(response.status_code, 400)
                self.assertIn("CSRF", response.get_data(as_text=True))

    def test_non_admin_cannot_add_person(self):
        response = self.post(
            "/admin/people",
            {"name": f"PG People Unauthorized {self.token}"},
            client=self.user_client,
        )
        self.assertEqual(response.status_code, 403)


    def test_admin_mobile_scanner_options_are_limited_to_own_and_managed_users(self):
        response = self.admin_client.get(
            "/api/scanner/mobile/options",
            base_url="http://localhost",
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        names = {person["name"] for person in response.get_json()["people"]}
        self.assertEqual(names, {self.source_name, self.target_name})

    def test_user_mobile_scanner_options_only_include_own_collection(self):
        response = self.user_client.get(
            "/api/scanner/mobile/options",
            base_url="http://localhost",
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        names = {person["name"] for person in response.get_json()["people"]}
        self.assertEqual(names, {self.target_name})


if __name__ == "__main__":
    unittest.main()
