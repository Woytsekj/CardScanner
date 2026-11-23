"""PostgreSQL integration tests for the main inventory page."""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from postgres_app_db import connect_app_database
from postgres_db import close_pool, open_pool, run_migrations


@unittest.skipUnless(
    os.environ.get("CARD_SCANNER_POSTGRES_DSN")
    and os.environ.get("CARD_SCANNER_DATABASE_BACKEND") == "postgres",
    "Run with the PostgreSQL backend and development DSN configured",
)
class PostgreSQLInventoryPageHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        open_pool()
        run_migrations()

        import app as app_module

        if app_module.DATABASE_BACKEND != "postgres":
            raise RuntimeError("Inventory page tests require PostgreSQL app mode")

        cls.app_module = app_module
        cls.app_module.app.config["TESTING"] = True

    @classmethod
    def tearDownClass(cls):
        close_pool()

    def setUp(self):
        self.token = uuid4().hex
        self.prefix = f"PGInvPage{self.token}"
        self.owner = f"PG Inventory Page Owner {self.token}"
        self.public_owner = f"PG Inventory Page Public Owner {self.token}"
        self.admin_username = f"pg-invpage-admin-{self.token}"
        self.csrf = f"csrf-invpage-{self.token}"
        self.inventory_names = []

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
            public_person = conn.execute(
                """
                INSERT INTO app.people (name, active, is_public)
                VALUES (?, TRUE, TRUE)
                RETURNING id
                """,
                (self.public_owner,),
            ).fetchone()
            self.public_person_id = int(public_person["id"])

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
                    "unused-inventory-page-admin-hash",
                    self.person_id,
                ),
            )

            base_time = datetime.now(timezone.utc)
            inventory_rows = []
            for index in range(105):
                name = f"{self.prefix} Card {index:03d}"
                self.inventory_names.append(name)
                inventory_rows.append((
                    name,
                    "PGI",
                    str(index),
                    1 + index % 3,
                    self.owner,
                    "foil" if index % 2 == 0 else "normal",
                    "PG Page Binder",
                    index % 3 == 0,
                    f"{0.25 + index / 100:.2f}",
                    base_time - timedelta(seconds=index),
                ))

            public_card_name = f"{self.prefix} Public Card"
            self.inventory_names.append(public_card_name)
            inventory_rows.append((
                public_card_name,
                "PGI",
                "PUB",
                2,
                self.public_owner,
                "normal",
                "PG Public Secret Shelf",
                False,
                "1.25",
                base_time + timedelta(seconds=2),
            ))

            outsider_name = f"{self.prefix} Card 999"
            self.inventory_names.append(outsider_name)
            inventory_rows.append((
                outsider_name,
                "PGI",
                "999",
                1,
                f"PG Other Owner {self.token}",
                "foil",
                "PG Page Binder",
                True,
                "9.99",
                base_time + timedelta(seconds=1),
            ))

            conn.executemany(
                """
                INSERT INTO app.inventory (
                    game_id, name, set_code, collector_number, quantity,
                    owner, finish, location, for_vending, price, scan_time
                )
                VALUES (
                    'mtg', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                inventory_rows,
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
            session["csrf_token"] = self.csrf

    def tearDown(self):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            conn.execute(
                "DELETE FROM app.inventory WHERE name LIKE ?",
                (self.prefix + "%",),
            )
            conn.execute(
                "DELETE FROM app.users WHERE username = ?",
                (self.admin_username,),
            )
            conn.execute(
                "DELETE FROM app.people WHERE id IN (?, ?)",
                (self.person_id, self.public_person_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def test_inventory_page_renders_and_hides_unmigrated_navigation(self):
        response = self.client.get("/", base_url="http://localhost")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

        html = response.get_data(as_text=True)
        self.assertIn(f"{self.prefix} Card 000", html)
        self.assertIn("Decks", html)
        self.assertNotIn("Mobile Scanner", html)
        self.assertNotIn("Download Desktop Scanner", html)
        self.assertIn("Export ManaBox", html)
        self.assertIn("Export CSV", html)
        self.assertIn(">Import CSV</button>", html)
        self.assertIn(
            f'name="csrf_token" value="{self.csrf}"',
            html,
        )
        self.assertIn('href="/vending"', html)

    def test_default_owner_scope_and_pagination(self):
        query = f"?q={self.prefix}&sort=name&dir=asc"
        first = self.client.get("/" + query, base_url="http://localhost")
        self.assertEqual(first.status_code, 200, first.get_data(as_text=True))

        html = first.get_data(as_text=True)
        self.assertIn("Search, filter, and manage your collection.", html)
        self.assertIn("Page 1 of 2", html)
        self.assertIn(f"{self.prefix} Card 000", html)
        self.assertIn(f"{self.prefix} Card 099", html)
        self.assertNotIn(f"{self.prefix} Card 100", html)
        self.assertNotIn(f"{self.prefix} Card 999", html)

        second = self.client.get(
            "/" + query + "&page=2",
            base_url="http://localhost",
        )
        self.assertEqual(second.status_code, 200, second.get_data(as_text=True))
        second_html = second.get_data(as_text=True)
        self.assertIn("Page 2 of 2", second_html)
        self.assertIn(f"{self.prefix} Card 100", second_html)
        self.assertIn(f"{self.prefix} Card 104", second_html)

    def test_explicit_all_owners_still_excludes_unassigned_other_owner_rows(self):
        response = self.client.get(
            f"/?q={self.prefix}&owner=&sort=name&dir=asc&page=2",
            base_url="http://localhost",
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        html = response.get_data(as_text=True)
        self.assertIn("Search, filter, and manage your collection.", html)
        self.assertNotIn(f"{self.prefix} Card 999", html)

    def test_public_cards_are_opt_in_read_only_and_hide_private_metadata(self):
        response = self.client.get(
            f"/?q={self.prefix}&public=1&view=list",
            base_url="http://localhost",
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        html = response.get_data(as_text=True)
        self.assertIn(f"{self.prefix} Public Card", html)
        self.assertNotIn(f"{self.prefix} Card 999", html)
        self.assertIn("Include public cards", html)
        self.assertIn("Public", html)
        self.assertIn("Not shared", html)
        self.assertNotIn("PG Public Secret Shelf", html)

        # The public toggle remains additive with a specific owner selection.
        selected_owner = self.client.get(
            f"/?q={self.prefix}&owner={self.owner.replace(' ', '+')}&public=1&view=list",
            base_url="http://localhost",
        )
        self.assertEqual(selected_owner.status_code, 200, selected_owner.get_data(as_text=True))
        selected_html = selected_owner.get_data(as_text=True)
        self.assertIn(f"{self.prefix} Public Card", selected_html)
        self.assertIn(f'name="owner"', selected_html)

        # Private metadata qualifiers cannot filter public rows or reveal values.
        private_filter = self.client.get(
            f"/?q={self.prefix}+loc:%22PG+Public+Secret+Shelf%22&public=1&view=list",
            base_url="http://localhost",
        )
        self.assertEqual(private_filter.status_code, 200, private_filter.get_data(as_text=True))
        private_filter_html = private_filter.get_data(as_text=True)
        self.assertIn(f"{self.prefix} Public Card", private_filter_html)
        self.assertNotIn("PG Public Secret Shelf", private_filter_html)
        self.assertIn("Location, deck, and vending filters are paused", private_filter_html)

        location_param = self.client.get(
            f"/?q={self.prefix}&location=PG+Public+Secret+Shelf&public=1&view=list",
            base_url="http://localhost",
        )
        self.assertEqual(location_param.status_code, 200, location_param.get_data(as_text=True))
        self.assertIn(f"{self.prefix} Public Card", location_param.get_data(as_text=True))

        row_start = html.index(f"{self.prefix} Public Card")
        row_start = html.rfind('<tr id="row-', 0, row_start)
        row_end = html.index("</tr>", row_start)
        public_row = html[row_start:row_end]
        self.assertNotIn("qty-input", public_row)
        self.assertNotIn("owner-input", public_row)
        self.assertNotIn("location-input", public_row)

        hidden = self.client.get(
            f"/?q={self.prefix}&view=list",
            base_url="http://localhost",
        )
        self.assertEqual(hidden.status_code, 200)
        self.assertNotIn(f"{self.prefix} Public Card", hidden.get_data(as_text=True))

    def test_account_owner_can_toggle_only_their_own_collection_public(self):
        preferences = self.client.get("/preferences", base_url="http://localhost")
        self.assertEqual(preferences.status_code, 200, preferences.get_data(as_text=True))
        self.assertIn("Make my collection public", preferences.get_data(as_text=True))

        enabled = self.client.post(
            "/account/collection-visibility",
            data={"csrf_token": self.csrf, "is_public": "1"},
            base_url="http://localhost",
        )
        self.assertEqual(enabled.status_code, 302)

        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            own = conn.execute(
                "SELECT is_public FROM app.people WHERE id = ?",
                (self.person_id,),
            ).fetchone()
            other = conn.execute(
                "SELECT is_public FROM app.people WHERE id = ?",
                (self.public_person_id,),
            ).fetchone()
        finally:
            conn.close()
        self.assertTrue(bool(own["is_public"]))
        self.assertTrue(bool(other["is_public"]))

        disabled = self.client.post(
            "/account/collection-visibility",
            data={"csrf_token": self.csrf},
            base_url="http://localhost",
        )
        self.assertEqual(disabled.status_code, 302)
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            own = conn.execute(
                "SELECT is_public FROM app.people WHERE id = ?",
                (self.person_id,),
            ).fetchone()
        finally:
            conn.close()
        self.assertFalse(bool(own["is_public"]))

    def test_combined_finish_location_and_vending_filters(self):
        response = self.client.get(
            f"/?q={self.prefix}&location=PG%20Page%20Binder"
            "&finish=foil&vending=yes&sort=name&dir=asc",
            base_url="http://localhost",
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        html = response.get_data(as_text=True)

        # Among the 105 owner rows, indices divisible by six satisfy
        # foil + vending=yes. The other owner's row is excluded by default.
        self.assertIn("Search, filter, and manage your collection.", html)
        self.assertIn(f"{self.prefix} Card 000", html)
        self.assertIn(f"{self.prefix} Card 006", html)
        self.assertNotIn(f"{self.prefix} Card 001", html)
        self.assertNotIn(f"{self.prefix} Card 999", html)


if __name__ == "__main__":
    unittest.main()
