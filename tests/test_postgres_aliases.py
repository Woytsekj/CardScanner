"""Integration and HTTP security tests for PostgreSQL card aliases."""

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
class PostgreSQLAliasHTTPTests(unittest.TestCase):
    # A known Scryfall printing in the imported development catalog.
    RESOLVED_SCRYFALL_ID = "0000579f-7b35-4ed3-b44c-db2a538066fe"

    @classmethod
    def setUpClass(cls):
        open_pool()
        run_migrations()

        import app as app_module

        if app_module.DATABASE_BACKEND != "postgres":
            raise RuntimeError("Alias HTTP tests require PostgreSQL app mode")

        cls.app_module = app_module
        cls.app_module.app.config["TESTING"] = True

    @classmethod
    def tearDownClass(cls):
        close_pool()

    def setUp(self):
        token = uuid4().hex
        self.token = token
        self.alias_ids = []
        self.admin_username = f"pg-alias-admin-{token}"
        self.user_username = f"pg-alias-user-{token}"
        self.scryfall_id = self.RESOLVED_SCRYFALL_ID
        self.unresolved_scryfall_id = f"pg-alias-unresolved-{token}"
        self.missing_scryfall_id = f"pg-alias-missing-{token}"
        self.inventory_ids = []

        self.alias_one = f"Alias fixture one {token}"
        self.alias_two = f"Alias fixture two {token}"

        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            conn.execute(
                """
                INSERT INTO app.users
                    (username, password_hash, is_admin, role,
                     is_superadmin, active)
                VALUES (?, ?, TRUE, 'admin', FALSE, TRUE)
                """,
                (self.admin_username, "unused-test-password-hash"),
            )
            conn.execute(
                """
                INSERT INTO app.users
                    (username, password_hash, is_admin, role,
                     is_superadmin, active)
                VALUES (?, ?, FALSE, 'user', FALSE, TRUE)
                """,
                (self.user_username, "unused-test-password-hash"),
            )

            for sid, name in (
                (self.scryfall_id, "PostgreSQL alias resolved fixture"),
                (self.unresolved_scryfall_id, "PostgreSQL alias unresolved fixture"),
            ):
                row = conn.execute(
                    """
                    INSERT INTO app.inventory (
                        game_id, scryfall_id, name, set_code,
                        quantity, owner
                    )
                    VALUES ('mtg', ?, ?, 'ZZT', 1, 'AliasTestOwner')
                    RETURNING id
                    """,
                    (sid, name),
                ).fetchone()
                self.inventory_ids.append(row["id"])

            conn.commit()
        finally:
            conn.close()

        self.admin_client = self.app_module.app.test_client()
        self.user_client = self.app_module.app.test_client()
        self.guest_client = self.app_module.app.test_client()

        with self.admin_client.session_transaction() as session:
            session["username"] = self.admin_username
            session["csrf_token"] = "csrf-admin-test-token"

        with self.user_client.session_transaction() as session:
            session["username"] = self.user_username
            session["csrf_token"] = "csrf-user-test-token"

    def tearDown(self):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            # Delete only aliases inserted by this test. The resolved
            # Scryfall printing is part of the imported catalog and may have
            # legitimate aliases that must remain untouched.
            for alias_id in set(self.alias_ids):
                conn.execute(
                    """
                    DELETE FROM app.card_aliases
                    WHERE id = ?
                      AND game_id = 'mtg'
                      AND provider_id = 'scryfall'
                    """,
                    (alias_id,),
                )
            for row_id in self.inventory_ids:
                conn.execute("DELETE FROM app.inventory WHERE id = ?", (row_id,))
            conn.execute(
                "DELETE FROM app.users WHERE username IN (?, ?)",
                (self.admin_username, self.user_username),
            )
            conn.commit()
        finally:
            conn.close()

    def post_alias(self, client, sid, alias, csrf="csrf-admin-test-token"):
        data = {"scryfall_id": sid, "alias": alias}
        if csrf is not None:
            data["csrf_token"] = csrf
        response = client.post(
            "/admin/aliases",
            data=data,
            base_url="http://localhost",
        )

        if response.status_code == 302 and csrf is not None:
            conn = connect_app_database(expected_database="cardscanner_dev")
            try:
                row = conn.execute(
                    """
                    SELECT id
                    FROM app.card_aliases
                    WHERE game_id = 'mtg'
                      AND provider_id = 'scryfall'
                      AND external_printing_id = ?
                      AND lower(alias) = lower(?)
                    """,
                    (sid, alias),
                ).fetchone()
                if row:
                    self.alias_ids.append(int(row["id"]))
            finally:
                conn.close()

        return response

    def insert_alias_directly(self, sid, alias):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            row = conn.execute(
                """
                INSERT INTO app.card_aliases (
                    game_id, provider_id, external_printing_id, alias
                )
                VALUES ('mtg', 'scryfall', ?, ?)
                RETURNING id
                """,
                (sid, alias),
            ).fetchone()
            conn.commit()
            alias_id = int(row["id"])
            self.alias_ids.append(alias_id)
            return alias_id
        finally:
            conn.close()

    def fetch_alias_rows(self, sid):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            return conn.execute(
                """
                SELECT id, alias, printing_id
                FROM app.card_aliases
                WHERE game_id = 'mtg'
                  AND provider_id = 'scryfall'
                  AND external_printing_id = ?
                """,
                (sid,),
            ).fetchall()
        finally:
            conn.close()

    def test_admin_can_list_aliases_in_legacy_response_shape(self):
        self.insert_alias_directly(self.scryfall_id, self.alias_one)
        self.insert_alias_directly(self.scryfall_id, self.alias_two)

        response = self.admin_client.get(
            f"/admin/aliases?scryfall_id={self.scryfall_id}",
            base_url="http://localhost",
        )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = response.get_json()
        self.assertTrue(payload["success"])

        listed = {item["alias"]: item for item in payload["aliases"]}
        self.assertIn(self.alias_one, listed)
        self.assertIn(self.alias_two, listed)

        for item in (listed[self.alias_one], listed[self.alias_two]):
            self.assertEqual(set(item), {"id", "alias", "created_at"})
            self.assertIsInstance(item["id"], int)
            self.assertRegex(
                item["created_at"],
                r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$",
            )

    def test_admin_without_printing_id_gets_empty_list(self):
        response = self.admin_client.get(
            "/admin/aliases",
            base_url="http://localhost",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["aliases"], [])

    def test_admin_can_add_alias_and_resolve_catalog_printing(self):
        alias = f"Resolved alias fixture {self.token}"
        response = self.post_alias(
            self.admin_client, self.scryfall_id, alias
        )
        self.assertEqual(response.status_code, 302)

        rows = self.fetch_alias_rows(self.scryfall_id)
        match = next(
            (row for row in rows if row["alias"] == alias),
            None,
        )
        self.assertIsNotNone(match)
        self.assertIsNotNone(match["printing_id"])

    def test_add_alias_preserves_unresolved_external_printing_id(self):
        alias = f"Unresolved alias fixture {self.token}"
        response = self.post_alias(
            self.admin_client,
            self.unresolved_scryfall_id,
            alias,
        )
        self.assertEqual(response.status_code, 302)

        rows = self.fetch_alias_rows(self.unresolved_scryfall_id)
        match = next(
            (row for row in rows if row["alias"] == alias),
            None,
        )
        self.assertIsNotNone(match)
        self.assertIsNone(match["printing_id"])

    def test_duplicate_alias_is_ignored_case_insensitively(self):
        alias = f"Case Insensitive Alias {self.token}"
        self.assertEqual(
            self.post_alias(
                self.admin_client, self.scryfall_id, alias
            ).status_code,
            302,
        )
        self.assertEqual(
            self.post_alias(
                self.admin_client, self.scryfall_id, alias.swapcase()
            ).status_code,
            302,
        )

        rows = self.fetch_alias_rows(self.scryfall_id)
        matches = [
            row for row in rows
            if row["alias"].casefold() == alias.casefold()
        ]
        self.assertEqual(len(matches), 1)

    def test_add_alias_requires_inventory_membership(self):
        response = self.post_alias(
            self.admin_client,
            self.missing_scryfall_id,
            "Orphan alias must not be saved",
        )
        self.assertEqual(response.status_code, 302)
        message = parse_qs(urlparse(response.location).query).get(
            "message", [""]
        )[0]
        self.assertEqual(message, "That card is not in your inventory.")

        self.assertEqual(self.fetch_alias_rows(self.missing_scryfall_id), [])

    def test_admin_can_delete_alias(self):
        alias_id = self.insert_alias_directly(
            self.scryfall_id, f"Delete fixture {uuid4().hex}"
        )

        response = self.admin_client.delete(
            f"/admin/aliases/{alias_id}",
            headers={"X-CSRFToken": "csrf-admin-test-token"},
            base_url="http://localhost",
        )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json(), {"success": True})
        self.assertFalse(
            any(row["id"] == alias_id for row in self.fetch_alias_rows(self.scryfall_id))
        )

    def test_deleting_missing_alias_returns_false(self):
        response = self.admin_client.delete(
            "/admin/aliases/999999999",
            headers={"X-CSRFToken": "csrf-admin-test-token"},
            base_url="http://localhost",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"success": False})

    def test_write_routes_require_csrf(self):
        post_response = self.post_alias(
            self.admin_client,
            self.scryfall_id,
            "Should fail without CSRF",
            csrf=None,
        )
        self.assertEqual(post_response.status_code, 400)

        alias_id = self.insert_alias_directly(
            self.scryfall_id, f"CSRF delete fixture {uuid4().hex}"
        )
        delete_response = self.admin_client.delete(
            f"/admin/aliases/{alias_id}",
            base_url="http://localhost",
        )
        self.assertEqual(delete_response.status_code, 400)
        self.assertTrue(
            any(row["id"] == alias_id for row in self.fetch_alias_rows(self.scryfall_id))
        )

    def test_guest_and_non_admin_cannot_list_aliases(self):
        path = f"/admin/aliases?scryfall_id={self.scryfall_id}"

        guest_response = self.guest_client.get(
            path, base_url="http://localhost"
        )
        self.assertEqual(guest_response.status_code, 302)
        self.assertIn("/login", guest_response.location)

        user_response = self.user_client.get(
            path, base_url="http://localhost"
        )
        self.assertEqual(user_response.status_code, 403)

    def test_guest_and_non_admin_cannot_add_aliases(self):
        guest_response = self.post_alias(
            self.guest_client,
            self.scryfall_id,
            "Guest must not add",
            csrf=None,
        )
        self.assertEqual(guest_response.status_code, 401)

        user_response = self.post_alias(
            self.user_client,
            self.scryfall_id,
            "Non-admin must not add",
            csrf="csrf-user-test-token",
        )
        self.assertEqual(user_response.status_code, 403)

    def test_guest_and_non_admin_cannot_delete_aliases(self):
        alias_id = self.insert_alias_directly(
            self.scryfall_id, f"Authorization fixture {uuid4().hex}"
        )

        guest_response = self.guest_client.delete(
            f"/admin/aliases/{alias_id}",
            base_url="http://localhost",
        )
        self.assertEqual(guest_response.status_code, 401)

        user_response = self.user_client.delete(
            f"/admin/aliases/{alias_id}",
            headers={"X-CSRFToken": "csrf-user-test-token"},
            base_url="http://localhost",
        )
        self.assertEqual(user_response.status_code, 403)

        self.assertTrue(
            any(row["id"] == alias_id for row in self.fetch_alias_rows(self.scryfall_id))
        )


if __name__ == "__main__":
    unittest.main()
