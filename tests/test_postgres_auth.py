"""Integration tests for PostgreSQL-backed CardScanner authentication."""
from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from postgres_app_db import connect_app_database
from postgres_auth import load_current_user


@unittest.skipUnless(
    os.environ.get("CARD_SCANNER_POSTGRES_DSN")
    and os.environ.get("CARD_SCANNER_DATABASE_BACKEND") == "postgres",
    "Run with the PostgreSQL backend and development DSN configured",
)
class PostgreSQLAuthenticationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from postgres_db import open_pool

        open_pool()

        # Import only after the test environment has selected PostgreSQL.
        import app as app_module

        if app_module.DATABASE_BACKEND != "postgres":
            raise RuntimeError("Authentication tests require PostgreSQL app mode")

        cls.app_module = app_module

    @classmethod
    def tearDownClass(cls):
        from postgres_db import close_pool

        close_pool()

    def setUp(self):
        self.token = uuid4().hex
        self.username = f"pg-auth-{self.token}"
        self.password = f"Test-Password-{self.token}"
        self.client_ip = (
            f"198.51.100.{int(self.token[:2], 16) % 254 + 1}"
        )
        self.ip_key = f"ip:{self.client_ip}"
        self.user_key = (
            f"user:{self.client_ip}|{self.username.strip().lower()[:150]}"
        )
        self.client = self.app_module.app.test_client()

        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            conn.execute(
                """
                INSERT INTO app.users
                    (username, password_hash, is_admin, role,
                     is_superadmin, active)
                VALUES (?, ?, TRUE, 'admin', FALSE, TRUE)
                """,
                (
                    self.username,
                    self.app_module.generate_password_hash(self.password),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            conn.execute(
                "DELETE FROM app.login_attempts WHERE key IN (?, ?)",
                (self.ip_key, self.user_key),
            )
            conn.execute(
                "DELETE FROM app.users WHERE username = ?",
                (self.username,),
            )
            conn.commit()
        finally:
            conn.close()

    def _csrf_token(self):
        response = self.client.get(
            "/login",
            environ_overrides={"REMOTE_ADDR": self.client_ip},
        )
        self.assertEqual(response.status_code, 200)

        with self.client.session_transaction() as session:
            token = session.get("csrf_token")

        self.assertTrue(token)
        return token

    def _post_login(self, password=None):
        token = self._csrf_token()
        return self.client.post(
            "/login",
            data={
                "username": self.username,
                "password": self.password if password is None else password,
                "csrf_token": token,
            },
            environ_overrides={"REMOTE_ADDR": self.client_ip},
        )

    def _rate_limit_rows(self):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            return conn.execute(
                """
                SELECT key, first_attempt, attempts, blocked_until
                FROM app.login_attempts
                WHERE key IN (?, ?)
                ORDER BY key
                """,
                (self.ip_key, self.user_key),
            ).fetchall()
        finally:
            conn.close()

    def test_valid_login_sets_session_and_updates_last_login(self):
        response = self._post_login()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/")

        with self.client.session_transaction() as session:
            self.assertEqual(session["username"], self.username)
            self.assertTrue(session["is_admin"])
            self.assertFalse(session["is_superadmin"])
            self.assertTrue(session.get("csrf_token"))

        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            row = conn.execute(
                "SELECT last_login_at FROM app.users WHERE username = ?",
                (self.username,),
            ).fetchone()
            self.assertIsNotNone(row["last_login_at"])
        finally:
            conn.close()

        self.assertEqual(self._rate_limit_rows(), [])

        # Successful admin login now reaches the PostgreSQL-backed inventory page.
        inventory_page = self.client.get(
            "/",
            environ_overrides={"REMOTE_ADDR": self.client_ip},
        )
        self.assertEqual(
            inventory_page.status_code,
            200,
            inventory_page.get_data(as_text=True),
        )
        self.assertIn(
            "MTG Scanner Inventory",
            inventory_page.get_data(as_text=True),
        )

    def test_invalid_password_records_failed_attempts(self):
        response = self._post_login("not-the-password")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(self._rate_limit_rows()), 2)
        self.assertTrue(
            all(int(row["attempts"]) == 1 for row in self._rate_limit_rows())
        )

        with self.client.session_transaction() as session:
            self.assertNotIn("username", session)

    def test_inactive_account_cannot_log_in(self):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            conn.execute(
                "UPDATE app.users SET active = FALSE WHERE username = ?",
                (self.username,),
            )
            conn.commit()
        finally:
            conn.close()

        response = self._post_login()

        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            self.assertNotIn("username", session)

        rows = self._rate_limit_rows()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(int(row["attempts"]) == 1 for row in rows))

    def test_active_rate_limit_blocks_login_without_incrementing(self):
        now = datetime.now(timezone.utc).timestamp()
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            conn.execute(
                """
                INSERT INTO app.login_attempts
                    (key, first_attempt, attempts, blocked_until)
                VALUES (?, ?, 5, ?)
                """,
                (self.user_key, now, now + 600),
            )
            conn.commit()
        finally:
            conn.close()

        response = self._post_login()

        self.assertEqual(response.status_code, 302)
        rows = self._rate_limit_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0]["attempts"]), 5)

        with self.client.session_transaction() as session:
            self.assertNotIn("username", session)

    def test_login_failure_can_be_cleared_after_success(self):
        from postgres_auth import record_failed_login

        now = datetime.now(timezone.utc).timestamp()
        record_failed_login(
            [self.ip_key, self.user_key],
            now,
            300,
            5,
            900,
        )
        self.assertEqual(len(self._rate_limit_rows()), 2)

        response = self._post_login()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._rate_limit_rows(), [])

    def test_existing_superadmin_role_is_preserved(self):
        user = load_current_user("ci-superadmin")

        self.assertIsNotNone(user)
        self.assertEqual(user["username"], "ci-superadmin")
        self.assertTrue(user["active"])
        self.assertTrue(user["is_admin"])
        self.assertTrue(user["is_superadmin"])
        self.assertEqual(user["role"], "superadmin")

    def test_login_does_not_rerun_application_bootstrap_markers(self):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            state_before = conn.execute(
                "SELECT key, value FROM app.app_state ORDER BY key"
            ).fetchall()
            migrations_before = conn.execute(
                "SELECT name, applied_at FROM app.app_migrations ORDER BY name"
            ).fetchall()
        finally:
            conn.close()

        response = self._post_login()
        self.assertEqual(response.status_code, 302)

        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            state_after = conn.execute(
                "SELECT key, value FROM app.app_state ORDER BY key"
            ).fetchall()
            migrations_after = conn.execute(
                "SELECT name, applied_at FROM app.app_migrations ORDER BY name"
            ).fetchall()
        finally:
            conn.close()

        self.assertEqual(state_after, state_before)
        self.assertEqual(migrations_after, migrations_before)

    def test_logout_requires_csrf_and_clears_session(self):
        login_response = self._post_login()
        self.assertEqual(login_response.status_code, 302)

        with self.client.session_transaction() as session:
            csrf = session["csrf_token"]
            self.assertEqual(session["username"], self.username)

        bad_logout = self.client.post(
            "/logout",
            environ_overrides={"REMOTE_ADDR": self.client_ip},
        )
        self.assertEqual(bad_logout.status_code, 400)

        logout = self.client.post(
            "/logout",
            data={"csrf_token": csrf},
            environ_overrides={"REMOTE_ADDR": self.client_ip},
        )
        self.assertEqual(logout.status_code, 302)

        with self.client.session_transaction() as session:
            self.assertNotIn("username", session)


if __name__ == "__main__":
    unittest.main()
