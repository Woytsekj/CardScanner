"""Integration tests for PostgreSQL-backed superadmin account management."""

from __future__ import annotations

import os
import unittest
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from werkzeug.security import check_password_hash

from postgres_app_db import connect_app_database
from postgres_db import close_pool, open_pool, run_migrations


@unittest.skipUnless(
    os.environ.get("CARD_SCANNER_POSTGRES_DSN")
    and os.environ.get("CARD_SCANNER_DATABASE_BACKEND") == "postgres",
    "Run with the PostgreSQL backend and development DSN configured",
)
class PostgreSQLUserAdminTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        open_pool()
        run_migrations()

        import app as app_module

        if app_module.DATABASE_BACKEND != "postgres":
            raise RuntimeError("User-admin tests require PostgreSQL app mode")

        cls.app_module = app_module
        cls.app_module.app.config["TESTING"] = True

    @classmethod
    def tearDownClass(cls):
        close_pool()

    def setUp(self):
        self.token = uuid4().hex
        self.csrf = f"csrf-user-admin-{self.token}"

        self.superadmin_username = f"pg-useradmin-super-{self.token}"
        self.admin_username = f"pg-useradmin-admin-{self.token}"
        self.vendor_username = f"pg-useradmin-vendor-{self.token}"

        self.active_person_name = f"PG UserAdmin Active {self.token}"
        self.inactive_person_name = f"PG UserAdmin Inactive {self.token}"

        self.usernames_to_clean = [
            self.superadmin_username,
            self.admin_username,
            self.vendor_username,
        ]
        self.person_ids_to_clean = []

        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            active_person = conn.execute(
                """
                INSERT INTO app.people (name, active)
                VALUES (?, TRUE)
                RETURNING id
                """,
                (self.active_person_name,),
            ).fetchone()
            inactive_person = conn.execute(
                """
                INSERT INTO app.people (name, active)
                VALUES (?, FALSE)
                RETURNING id
                """,
                (self.inactive_person_name,),
            ).fetchone()

            self.active_person_id = int(active_person["id"])
            self.inactive_person_id = int(inactive_person["id"])
            self.person_ids_to_clean.extend(
                [self.active_person_id, self.inactive_person_id]
            )

            conn.execute(
                """
                INSERT INTO app.users (
                    username, password_hash, is_admin, role,
                    is_superadmin, active
                )
                VALUES (?, ?, TRUE, 'superadmin', TRUE, TRUE)
                """,
                (self.superadmin_username, "unused-test-password-hash"),
            )
            conn.execute(
                """
                INSERT INTO app.users (
                    username, password_hash, is_admin, role,
                    is_superadmin, active
                )
                VALUES (?, ?, TRUE, 'admin', FALSE, TRUE)
                """,
                (self.admin_username, "unused-test-password-hash"),
            )
            conn.execute(
                """
                INSERT INTO app.users (
                    username, password_hash, is_admin, role,
                    is_superadmin, active
                )
                VALUES (?, ?, FALSE, 'vendor', FALSE, TRUE)
                """,
                (self.vendor_username, "unused-test-password-hash"),
            )
            conn.commit()
        finally:
            conn.close()

        self.superadmin_client = self.app_module.app.test_client()
        self.admin_client = self.app_module.app.test_client()
        self.vendor_client = self.app_module.app.test_client()
        self.guest_client = self.app_module.app.test_client()

        for client, username in (
            (self.superadmin_client, self.superadmin_username),
            (self.admin_client, self.admin_username),
            (self.vendor_client, self.vendor_username),
        ):
            with client.session_transaction() as session:
                session["username"] = username
                session["csrf_token"] = self.csrf

    def tearDown(self):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            # Delete only accounts and people created by this test.
            for username in self.usernames_to_clean:
                conn.execute(
                    "DELETE FROM app.users WHERE username = ?",
                    (username,),
                )

            for person_id in self.person_ids_to_clean:
                conn.execute(
                    "DELETE FROM app.people WHERE id = ?",
                    (person_id,),
                )

            conn.commit()
        finally:
            conn.close()

    def track_username(self, username):
        if username not in self.usernames_to_clean:
            self.usernames_to_clean.append(username)

    def post(self, path, data=None, *, client=None, csrf=True):
        payload = dict(data or {})
        if csrf:
            payload["csrf_token"] = self.csrf

        return (client or self.superadmin_client).post(
            path,
            data=payload,
            base_url="http://localhost",
        )

    @staticmethod
    def message(response):
        return parse_qs(urlparse(response.location).query).get(
            "message", [""]
        )[0]

    def fetch_user(self, username):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            return conn.execute(
                """
                SELECT username, password_hash, is_admin, role,
                       is_superadmin, active, person_id
                FROM app.users
                WHERE lower(btrim(username)) = lower(btrim(?))
                LIMIT 1
                """,
                (username,),
            ).fetchone()
        finally:
            conn.close()

    def test_admin_page_shows_web_users_but_hides_unsupported_panels(self):
        response = self.superadmin_client.get(
            "/admin",
            base_url="http://localhost",
        )

        self.assertEqual(
            response.status_code, 200, response.get_data(as_text=True)
        )
        html = response.get_data(as_text=True)

        self.assertIn("Web users</h2>", html)
        self.assertIn("Create User", html)
        self.assertIn("Reset password", html)
        self.assertIn(self.superadmin_username, html)
        self.assertIn(self.admin_username, html)
        self.assertIn(self.vendor_username, html)

        self.assertNotIn("Admin password</h2>", html)
        self.assertNotIn('action="/admin/password"', html)
        self.assertNotIn("Price History &amp; Discord Alerts", html)
        self.assertIn("Scryfall card metadata", html)
        self.assertIn("Download / Refresh Scryfall Bulk Metadata", html)
        self.assertIn('action="/admin/enrich_metadata"', html)
        self.assertIn('action="/admin/refresh_scryfall_bulk"', html)
        self.assertNotIn("Back to inventory", html)

    def test_create_user_hashes_password_and_assigns_active_person(self):
        username = f"pg-useradmin-created-{self.token}"
        password = f"Created-Password-{self.token}"
        self.track_username(username)

        response = self.post(
            "/admin/users",
            {
                "username": username,
                "password": password,
                "role": "admin",
                "person_id": str(self.active_person_id),
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn(f"Created admin user {username}", self.message(response))

        user = self.fetch_user(username)
        self.assertIsNotNone(user)
        self.assertTrue(user["is_admin"])
        self.assertEqual(user["role"], "admin")
        self.assertFalse(user["is_superadmin"])
        self.assertTrue(user["active"])
        self.assertEqual(user["person_id"], self.active_person_id)
        self.assertTrue(check_password_hash(user["password_hash"], password))

    def test_create_user_rejects_short_password_and_inactive_person(self):
        short_password = self.post(
            "/admin/users",
            {
                "username": f"pg-useradmin-short-{self.token}",
                "password": "too-short",
                "role": "vendor",
            },
        )
        self.assertEqual(short_password.status_code, 302)
        self.assertEqual(
            self.message(short_password),
            "User password must be at least 12 characters.",
        )

        inactive_person = self.post(
            "/admin/users",
            {
                "username": f"pg-useradmin-inactive-{self.token}",
                "password": f"Long-Enough-Password-{self.token}",
                "role": "vendor",
                "person_id": str(self.inactive_person_id),
            },
        )
        self.assertEqual(inactive_person.status_code, 302)
        self.assertEqual(
            self.message(inactive_person),
            "Choose an active person for this account.",
        )

        missing_person = self.post(
            "/admin/users",
            {
                "username": f"pg-useradmin-missing-{self.token}",
                "password": f"Long-Enough-Password-{self.token}",
                "role": "vendor",
                "person_id": "999999999",
            },
        )
        self.assertEqual(missing_person.status_code, 302)
        self.assertEqual(
            self.message(missing_person),
            "Choose an active person for this account.",
        )

    def test_create_user_rejects_case_insensitive_duplicate_username(self):
        response = self.post(
            "/admin/users",
            {
                "username": self.vendor_username.upper(),
                "password": f"Long-Enough-Password-{self.token}",
                "role": "vendor",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self.message(response),
            "That username already exists.",
        )
        self.assertIsNotNone(self.fetch_user(self.vendor_username))

    def test_create_user_rejects_person_already_assigned_to_another_account(self):
        assigned = self.post(
            f"/admin/users/{self.admin_username}/person",
            {"person_id": str(self.active_person_id)},
        )
        self.assertEqual(assigned.status_code, 302)

        username = f"pg-useradmin-person-conflict-{self.token}"
        response = self.post(
            "/admin/users",
            {
                "username": username,
                "password": f"Long-Enough-Password-{self.token}",
                "role": "vendor",
                "person_id": str(self.active_person_id),
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self.message(response),
            "That person is already assigned to another account.",
        )
        self.assertIsNone(self.fetch_user(username))

    def test_assign_person_enforces_unique_assignment_and_can_unlink(self):
        assigned = self.post(
            f"/admin/users/{self.admin_username}/person",
            {"person_id": str(self.active_person_id)},
        )
        self.assertEqual(assigned.status_code, 302)
        self.assertEqual(
            self.message(assigned),
            f"Linked {self.admin_username} to {self.active_person_name}.",
        )
        self.assertEqual(
            self.fetch_user(self.admin_username)["person_id"],
            self.active_person_id,
        )

        conflict = self.post(
            f"/admin/users/{self.vendor_username}/person",
            {"person_id": str(self.active_person_id)},
        )
        self.assertEqual(conflict.status_code, 302)
        self.assertIn("already linked", self.message(conflict))
        self.assertIsNone(self.fetch_user(self.vendor_username)["person_id"])

        unlinked = self.post(
            f"/admin/users/{self.admin_username}/person",
            {"person_id": ""},
        )
        self.assertEqual(unlinked.status_code, 302)
        self.assertEqual(
            self.message(unlinked),
            f"Removed the person assignment for {self.admin_username}.",
        )
        self.assertIsNone(self.fetch_user(self.admin_username)["person_id"])

    def test_assign_person_rejects_inactive_or_invalid_person(self):
        inactive = self.post(
            f"/admin/users/{self.admin_username}/person",
            {"person_id": str(self.inactive_person_id)},
        )
        self.assertEqual(inactive.status_code, 302)
        self.assertEqual(self.message(inactive), "Choose an active person.")

        invalid = self.post(
            f"/admin/users/{self.admin_username}/person",
            {"person_id": "not-an-integer"},
        )
        self.assertEqual(invalid.status_code, 302)
        self.assertEqual(
            self.message(invalid),
            "Invalid person assignment.",
        )

    def test_toggle_user_activates_and_deactivates_other_account(self):
        deactivated = self.post(
            f"/admin/users/{self.vendor_username}/toggle"
        )
        self.assertEqual(deactivated.status_code, 302)
        self.assertEqual(
            self.message(deactivated),
            f"Deactivated {self.vendor_username}.",
        )
        self.assertFalse(self.fetch_user(self.vendor_username)["active"])

        activated = self.post(
            f"/admin/users/{self.vendor_username}/toggle"
        )
        self.assertEqual(activated.status_code, 302)
        self.assertEqual(
            self.message(activated),
            f"Activated {self.vendor_username}.",
        )
        self.assertTrue(self.fetch_user(self.vendor_username)["active"])

    def test_toggle_user_refuses_to_deactivate_current_account(self):
        response = self.post(
            f"/admin/users/{self.superadmin_username}/toggle"
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self.message(response),
            "You cannot deactivate your own account.",
        )
        self.assertTrue(self.fetch_user(self.superadmin_username)["active"])

    def test_admin_password_reset_updates_hash_and_validates_length(self):
        new_password = f"Reset-Password-{self.token}"
        response = self.post(
            f"/admin/users/{self.vendor_username}/password",
            {"password": new_password},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.message(response), "Password reset.")

        user = self.fetch_user(self.vendor_username)
        self.assertTrue(
            check_password_hash(user["password_hash"], new_password)
        )

        old_hash = user["password_hash"]
        too_short = self.post(
            f"/admin/users/{self.vendor_username}/password",
            {"password": "short"},
        )
        self.assertEqual(too_short.status_code, 302)
        self.assertEqual(
            self.message(too_short),
            "Password must be at least 12 characters.",
        )
        self.assertEqual(
            self.fetch_user(self.vendor_username)["password_hash"],
            old_hash,
        )

    def test_admin_password_reset_reports_missing_user(self):
        response = self.post(
            f"/admin/users/not-present-{self.token}/password",
            {"password": f"Long-Enough-Password-{self.token}"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.message(response), "User not found.")

    def test_rename_user_updates_username_and_self_session(self):
        new_username = f"pg-useradmin-renamed-{self.token}"
        response = self.post(
            f"/admin/users/{self.vendor_username}/rename",
            {"new_username": new_username},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self.message(response),
            f"Renamed {self.vendor_username} to {new_username}.",
        )
        self.assertIsNone(self.fetch_user(self.vendor_username))
        self.assertIsNotNone(self.fetch_user(new_username))
        self.track_username(new_username)

        self_name = f"pg-useradmin-super-renamed-{self.token}"
        self_response = self.post(
            f"/admin/users/{self.superadmin_username}/rename",
            {"new_username": self_name},
        )
        self.assertEqual(self_response.status_code, 302)
        self.assertEqual(
            self.message(self_response),
            f"Renamed {self.superadmin_username} to {self_name}.",
        )

        self.usernames_to_clean.remove(self.superadmin_username)
        self.superadmin_username = self_name
        self.track_username(self_name)

        with self.superadmin_client.session_transaction() as session:
            self.assertEqual(session["username"], self_name)

        self.assertTrue(self.fetch_user(self_name)["is_superadmin"])

    def test_rename_user_rejects_short_and_duplicate_names(self):
        short = self.post(
            f"/admin/users/{self.vendor_username}/rename",
            {"new_username": "x"},
        )
        self.assertEqual(short.status_code, 302)
        self.assertEqual(
            self.message(short),
            "Username must be at least 2 characters.",
        )

        duplicate = self.post(
            f"/admin/users/{self.vendor_username}/rename",
            {"new_username": self.admin_username.upper()},
        )
        self.assertEqual(duplicate.status_code, 302)
        self.assertEqual(
            self.message(duplicate),
            "That username already exists.",
        )
        self.assertIsNotNone(self.fetch_user(self.vendor_username))

    def test_change_user_role_updates_role_flags(self):
        promoted = self.post(
            f"/admin/users/{self.vendor_username}/role",
            {"role": "admin"},
        )
        self.assertEqual(promoted.status_code, 302)
        self.assertEqual(
            self.message(promoted),
            f"Updated {self.vendor_username} to admin.",
        )

        user = self.fetch_user(self.vendor_username)
        self.assertEqual(user["role"], "admin")
        self.assertTrue(user["is_admin"])

        demoted = self.post(
            f"/admin/users/{self.vendor_username}/role",
            {"role": "vendor"},
        )
        self.assertEqual(demoted.status_code, 302)
        self.assertEqual(
            self.message(demoted),
            f"Updated {self.vendor_username} to vendor.",
        )

        user = self.fetch_user(self.vendor_username)
        self.assertEqual(user["role"], "vendor")
        self.assertFalse(user["is_admin"])

    def test_change_user_role_cannot_demote_superadmin(self):
        response = self.post(
            f"/admin/users/{self.superadmin_username}/role",
            {"role": "vendor"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self.message(response),
            "The superadmin account cannot be changed to a normal role.",
        )
        user = self.fetch_user(self.superadmin_username)
        self.assertTrue(user["is_superadmin"])
        self.assertEqual(user["role"], "superadmin")

    def test_change_user_role_invalid_value_defaults_to_vendor(self):
        response = self.post(
            f"/admin/users/{self.admin_username}/role",
            {"role": "unexpected"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self.message(response),
            f"Updated {self.admin_username} to vendor.",
        )
        user = self.fetch_user(self.admin_username)
        self.assertEqual(user["role"], "vendor")
        self.assertFalse(user["is_admin"])

    def test_delete_user_removes_account_and_cannot_delete_self(self):
        response = self.post(
            f"/admin/users/{self.vendor_username}/delete"
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self.message(response),
            f"Deleted web user {self.vendor_username} permanently.",
        )
        self.assertIsNone(self.fetch_user(self.vendor_username))

        self_response = self.post(
            f"/admin/users/{self.superadmin_username}/delete"
        )
        self.assertEqual(self_response.status_code, 302)
        self.assertEqual(
            self.message(self_response),
            "You cannot delete your own account.",
        )
        self.assertIsNotNone(self.fetch_user(self.superadmin_username))

    def test_delete_other_superadmin_when_another_remains(self):
        target = f"pg-useradmin-extra-super-{self.token}"
        self.track_username(target)

        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            conn.execute(
                """
                INSERT INTO app.users (
                    username, password_hash, is_admin, role,
                    is_superadmin, active
                )
                VALUES (?, ?, TRUE, 'superadmin', TRUE, TRUE)
                """,
                (target, "unused-test-password-hash"),
            )
            conn.commit()
        finally:
            conn.close()

        response = self.post(f"/admin/users/{target}/delete")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self.message(response),
            f"Deleted web user {target} permanently.",
        )
        self.assertIsNone(self.fetch_user(target))

    def test_non_superadmin_cannot_create_or_manage_accounts(self):
        requests = (
            (
                "/admin/users",
                {
                    "username": f"pg-useradmin-denied-{self.token}",
                    "password": f"Long-Enough-Password-{self.token}",
                    "role": "vendor",
                },
            ),
            (
                f"/admin/users/{self.vendor_username}/toggle",
                {},
            ),
            (
                f"/admin/users/{self.vendor_username}/password",
                {"password": f"Long-Enough-Password-{self.token}"},
            ),
            (
                f"/admin/users/{self.vendor_username}/role",
                {"role": "admin"},
            ),
            (
                f"/admin/users/{self.vendor_username}/delete",
                {},
            ),
        )

        for path, payload in requests:
            with self.subTest(path=path):
                response = self.post(
                    path,
                    payload,
                    client=self.admin_client,
                )
                self.assertEqual(response.status_code, 403)

    def test_all_eight_user_write_routes_require_csrf(self):
        requests = (
            ("/admin/users", {
                "username": f"pg-useradmin-no-csrf-{self.token}",
                "password": f"Long-Enough-Password-{self.token}",
                "role": "vendor",
            }),
            (f"/admin/users/{self.vendor_username}/person", {}),
            (f"/admin/users/{self.vendor_username}/toggle", {}),
            (f"/admin/users/{self.vendor_username}/password", {
                "password": f"Long-Enough-Password-{self.token}",
            }),
            (f"/admin/users/{self.vendor_username}/rename", {
                "new_username": f"pg-useradmin-no-csrf-renamed-{self.token}",
            }),
            (f"/admin/users/{self.vendor_username}/role", {
                "role": "admin",
            }),
            (f"/admin/users/{self.vendor_username}/delete", {}),
            ("/admin/password", {
                "password": f"CSRF-Test-Password-{self.token}",
            }),
        )

        for path, payload in requests:
            with self.subTest(path=path):
                response = self.post(path, payload, csrf=False)
                self.assertEqual(response.status_code, 400)
                self.assertIn("CSRF", response.get_data(as_text=True))

    def test_self_service_password_change_updates_current_account(self):
        new_password = f"Self-Service-Password-{self.token}"
        response = self.post(
            "/admin/password",
            {"password": new_password},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.message(response), "Password updated.")

        user = self.fetch_user(self.superadmin_username)
        self.assertTrue(
            check_password_hash(user["password_hash"], new_password)
        )



    def test_password_change_rejects_short_password(self):
        before = self.fetch_user(
            self.superadmin_username
        )["password_hash"]

        response = self.post(
            "/admin/password",
            {"password": "too-short"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self.message(response),
            "Password must be at least 12 characters.",
        )
        self.assertEqual(
            self.fetch_user(self.superadmin_username)["password_hash"],
            before,
        )

    def test_regular_admin_can_change_own_password(self):
        new_password = f"Regular-Admin-Password-{self.token}"
        response = self.post(
            "/admin/password",
            {"password": new_password},
            client=self.admin_client,
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.message(response), "Password updated.")
        self.assertTrue(
            check_password_hash(
                self.fetch_user(self.admin_username)["password_hash"],
                new_password,
            )
        )

    def test_vendor_cannot_change_password_through_admin_route(self):
        response = self.post(
            "/admin/password",
            {"password": f"Vendor-Password-Change-{self.token}"},
            client=self.vendor_client,
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            self.fetch_user(self.vendor_username)["password_hash"],
            "unused-test-password-hash",
        )



if __name__ == "__main__":
    unittest.main()
