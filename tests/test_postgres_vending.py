"""PostgreSQL integration tests for vending pages, exports, and labels."""

from __future__ import annotations

import csv
import io
import os
import unittest
from uuid import uuid4

from postgres_app_db import connect_app_database
from postgres_db import close_pool, open_pool, run_migrations


@unittest.skipUnless(
    os.environ.get("CARD_SCANNER_POSTGRES_DSN")
    and os.environ.get("CARD_SCANNER_DATABASE_BACKEND") == "postgres",
    "Run with the PostgreSQL backend and development DSN configured",
)
class PostgreSQLVendingHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        open_pool()
        run_migrations()

        import app as app_module

        if app_module.DATABASE_BACKEND != "postgres":
            raise RuntimeError("Vending tests require PostgreSQL app mode")

        cls.app_module = app_module
        cls.app_module.app.config["TESTING"] = True

    @classmethod
    def tearDownClass(cls):
        close_pool()

    def setUp(self):
        self.token = uuid4().hex
        self.prefix = f"PGVending{self.token}"
        self.owner = f"PG Vending Owner {self.token}"
        self.other_owner = f"PG Vending Other {self.token}"

        self.admin_username = f"pg-vending-admin-{self.token}"
        self.vendor_username = f"pg-vending-vendor-{self.token}"
        self.user_username = f"pg-vending-user-{self.token}"

        self.alpha_name = f"{self.prefix} Alpha"
        self.beta_name = f"{self.prefix} Beta"
        self.other_name = f"{self.prefix} Other Owner"
        self.excluded_name = f"{self.prefix} Not Vending"
        self.item_ids = {}

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

            users = (
                (self.admin_username, True, "admin"),
                (self.vendor_username, False, "vendor"),
                (self.user_username, False, "user"),
            )
            for username, is_admin, role in users:
                conn.execute(
                    """
                    INSERT INTO app.users (
                        username, password_hash, is_admin, role,
                        is_superadmin, active
                    )
                    VALUES (?, ?, ?, ?, FALSE, TRUE)
                    """,
                    (
                        username,
                        "unused-vending-test-password-hash",
                        is_admin,
                        role,
                    ),
                )

            inventory_rows = (
                (
                    "alpha", self.alpha_name, "PGV", "1", 2,
                    self.owner, "normal", "Vending Binder", True, "2.50",
                ),
                (
                    "beta", self.beta_name, "PGV", "2", 1,
                    self.owner, "foil", "Vending Binder", True, "7.25",
                ),
                (
                    "other", self.other_name, "PGV", "3", 4,
                    self.other_owner, "normal", "Shelf B", True, "1.25",
                ),
                (
                    "excluded", self.excluded_name, "PGV", "4", 1,
                    self.owner, "normal", "Vending Binder", False, "99.00",
                ),
            )

            for (
                key, name, set_code, collector_number, quantity,
                owner, finish, location, for_vending, price,
            ) in inventory_rows:
                row = conn.execute(
                    """
                    INSERT INTO app.inventory (
                        game_id, name, set_code, collector_number, quantity,
                        owner, finish, location, for_vending, price
                    )
                    VALUES ('mtg', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    RETURNING id
                    """,
                    (
                        name,
                        set_code,
                        collector_number,
                        quantity,
                        owner,
                        finish,
                        location,
                        for_vending,
                        price,
                    ),
                ).fetchone()
                self.item_ids[key] = int(row["id"])

            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        self.admin_client = self.make_client(self.admin_username)
        self.vendor_client = self.make_client(self.vendor_username)
        self.user_client = self.make_client(self.user_username)
        self.guest_client = self.app_module.app.test_client()

    def make_client(self, username):
        client = self.app_module.app.test_client()
        with client.session_transaction() as session:
            session["username"] = username
            session["csrf_token"] = f"csrf-vending-{self.token}"
        return client

    def tearDown(self):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            conn.execute(
                "DELETE FROM app.inventory WHERE name LIKE ?",
                (self.prefix + "%",),
            )
            conn.execute(
                """
                DELETE FROM app.users
                WHERE username IN (?, ?, ?)
                """,
                (
                    self.admin_username,
                    self.vendor_username,
                    self.user_username,
                ),
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

    def test_vending_page_filters_eligible_rows_by_owner_and_total_value(self):
        response = self.admin_client.get(
            "/vending",
            query_string={"q": self.prefix},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

        html = response.get_data(as_text=True)
        self.assertIn("3 matching rows", html)
        self.assertIn(self.alpha_name, html)
        self.assertIn(self.beta_name, html)
        self.assertIn(self.other_name, html)
        self.assertNotIn(self.excluded_name, html)
        self.assertIn(self.owner, html)
        self.assertIn('href="/vending/export.csv?', html)

        filtered = self.admin_client.get(
            "/vending",
            query_string={
                "q": self.prefix,
                "owner": self.owner,
                "min_value": "6.00",
            },
        )
        self.assertEqual(
            filtered.status_code,
            200,
            filtered.get_data(as_text=True),
        )
        filtered_html = filtered.get_data(as_text=True)
        self.assertIn("1 matching rows", filtered_html)
        self.assertIn(self.beta_name, filtered_html)
        self.assertNotIn(self.alpha_name, filtered_html)
        self.assertNotIn(self.other_name, filtered_html)
        self.assertNotIn(self.excluded_name, filtered_html)

    def test_vendor_csv_preserves_columns_prices_and_vending_filter(self):
        response = self.admin_client.get(
            "/vending/export.csv",
            query_string={
                "q": self.prefix,
                "owner": self.owner,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/csv")

        rows = list(csv.DictReader(io.StringIO(response.get_data(as_text=True))))
        self.assertEqual(
            list(rows[0].keys()),
            [
                "name",
                "set_code",
                "collector_number",
                "rarity",
                "finish",
                "owner",
                "quantity",
                "location",
                "unit_price",
                "total_value",
                "tcgplayer_id",
            ],
        )

        by_name = {row["name"]: row for row in rows}
        self.assertEqual(set(by_name), {self.alpha_name, self.beta_name})
        self.assertEqual(by_name[self.alpha_name]["quantity"], "2")
        self.assertEqual(by_name[self.alpha_name]["unit_price"], "2.50")
        self.assertEqual(by_name[self.alpha_name]["total_value"], "5.00")
        self.assertEqual(by_name[self.beta_name]["finish"], "foil")
        self.assertEqual(by_name[self.beta_name]["unit_price"], "7.25")
        self.assertEqual(by_name[self.beta_name]["total_value"], "7.25")

    def test_label_generation_deduplicates_ids_and_repeats_quantity(self):
        selected_ids = ",".join(
            str(value)
            for value in (
                self.item_ids["alpha"],
                self.item_ids["alpha"],
                self.item_ids["beta"],
                self.item_ids["excluded"],
                999999999999,
            )
        )
        response = self.admin_client.get(
            "/vending/labels",
            query_string={
                "ids": selected_ids,
                "template": "5160",
                "repeat": "qty",
                "start": "2",
            },
        )
        self.assertEqual(
            response.status_code,
            200,
            response.get_data(as_text=True),
        )

        html = response.get_data(as_text=True)
        self.assertIn("Selected 4 · Printing 3 label(s)", html)
        self.assertEqual(
            html.count(f'<div class="label-name">{self.alpha_name}</div>'),
            2,
        )
        self.assertEqual(
            html.count(f'<div class="label-name">{self.beta_name}</div>'),
            1,
        )
        self.assertNotIn(self.excluded_name, html)
        self.assertIn(
            "Some selected cards are no longer available for vending and were skipped.",
            html,
        )

    def test_admin_and_vendor_can_access_vending_but_regular_user_cannot(self):
        label_path = f"/vending/labels?ids={self.item_ids['alpha']}"

        for client in (self.admin_client, self.vendor_client):
            with self.subTest(client=client):
                self.assertEqual(client.get("/vending").status_code, 200)
                self.assertEqual(
                    client.get("/vending/export.csv").status_code,
                    200,
                )
                self.assertEqual(client.get(label_path).status_code, 200)

        vendor_html = self.vendor_client.get(
            "/vending"
        ).get_data(as_text=True)
        self.assertIn(">Inventory</a>", vendor_html)
        self.assertNotIn(">Decks</a>", vendor_html)
        self.assertNotIn('id="vendorBulkEditBar"', vendor_html)

        # Vendors can browse inventory/public cards, but have no inventory edit mode.
        inventory = self.vendor_client.get("/")
        self.assertEqual(inventory.status_code, 200, inventory.get_data(as_text=True))
        inventory_html = inventory.get_data(as_text=True)
        self.assertIn("Include public cards", inventory_html)
        self.assertNotIn("Edit mode", inventory_html)

        preferences = self.vendor_client.get("/preferences")
        self.assertEqual(preferences.status_code, 200, preferences.get_data(as_text=True))
        self.assertIn("Make my collection public", preferences.get_data(as_text=True))

        for path in (
            "/vending",
            "/vending/export.csv",
            label_path,
        ):
            with self.subTest(path=path):
                self.assertEqual(self.user_client.get(path).status_code, 403)
                self.assertEqual(self.guest_client.get(path).status_code, 302)


if __name__ == "__main__":
    unittest.main()
