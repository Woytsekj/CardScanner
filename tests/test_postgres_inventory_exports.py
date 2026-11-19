"""PostgreSQL integration tests for inventory CSV exports."""

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
class PostgreSQLInventoryExportHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        open_pool()
        run_migrations()

        import app as app_module

        if app_module.DATABASE_BACKEND != "postgres":
            raise RuntimeError("Inventory export tests require PostgreSQL app mode")

        cls.app_module = app_module
        cls.app_module.app.config["TESTING"] = True

    @classmethod
    def tearDownClass(cls):
        close_pool()

    def setUp(self):
        self.token = uuid4().hex
        self.prefix = f"PGExport{self.token}"
        self.owner = f"PG Export Owner {self.token}"
        self.other_owner = f"PG Export Other {self.token}"

        self.admin_username = f"pg-export-admin-{self.token}"
        self.vendor_username = f"pg-export-vendor-{self.token}"
        self.user_username = f"pg-export-user-{self.token}"

        self.normal_name = f"{self.prefix} Normal"
        self.foil_name = f"{self.prefix} Foil"

        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            for username, is_admin, role in (
                (self.admin_username, True, "admin"),
                (self.vendor_username, False, "vendor"),
                (self.user_username, False, "user"),
            ):
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
                        "unused-export-test-password-hash",
                        is_admin,
                        role,
                    ),
                )

            conn.execute(
                """
                INSERT INTO app.inventory (
                    game_id, name, set_code, rarity, price,
                    quantity, owner, finish, collector_number,
                    for_vending
                )
                VALUES
                    ('mtg', ?, 'EXP', 'Mythic', '2.50',
                     2, ?, 'normal', '101', FALSE),
                    ('mtg', ?, 'EXP', 'Rare', '7.25',
                     3, ?, 'foil', '202', TRUE)
                """,
                (
                    self.normal_name,
                    self.owner,
                    self.foil_name,
                    self.other_owner,
                ),
            )
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
            session["csrf_token"] = f"csrf-export-{self.token}"
        return client

    def tearDown(self):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            conn.execute(
                "DELETE FROM app.inventory WHERE name LIKE ?",
                (self.prefix + "%",),
            )
            conn.execute(
                "DELETE FROM app.users WHERE username IN (?, ?, ?)",
                (
                    self.admin_username,
                    self.vendor_username,
                    self.user_username,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def test_standard_export_preserves_columns_prices_and_all_owners(self):
        response = self.admin_client.get("/export_csv")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/csv")

        rows = list(csv.DictReader(io.StringIO(response.get_data(as_text=True))))
        by_name = {
            row["name"]: row
            for row in rows
            if row["name"] and row["name"].startswith(self.prefix)
        }

        self.assertEqual(set(by_name), {self.normal_name, self.foil_name})
        self.assertEqual(
            list(rows[0].keys()),
            [
                "id",
                "scryfall_id",
                "name",
                "set_code",
                "rarity",
                "scanned_price",
                "finish",
                "quantity",
                "owner",
                "collector_number",
                "tcgplayer_id",
                "location",
                "for_vending",
                "current_normal",
                "current_foil",
                "current_unit_value",
                "current_total_value",
                "scan_time",
            ],
        )

        normal = by_name[self.normal_name]
        self.assertEqual(normal["set_code"], "EXP")
        self.assertEqual(normal["quantity"], "2")
        self.assertEqual(normal["owner"], self.owner)
        self.assertEqual(normal["for_vending"], "0")
        self.assertEqual(float(normal["current_unit_value"]), 2.50)
        self.assertEqual(float(normal["current_total_value"]), 5.00)

        foil = by_name[self.foil_name]
        self.assertEqual(foil["finish"], "foil")
        self.assertEqual(foil["for_vending"], "1")
        self.assertEqual(foil["owner"], self.other_owner)
        self.assertEqual(float(foil["current_unit_value"]), 7.25)
        self.assertEqual(float(foil["current_total_value"]), 21.75)

    def test_manabox_export_filters_owner_and_preserves_format(self):
        response = self.admin_client.get(
            "/export_manabox_csv",
            query_string={"owner": self.owner},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/csv")

        rows = list(csv.DictReader(io.StringIO(response.get_data(as_text=True))))
        self.assertEqual(
            list(rows[0].keys()),
            [
                "Name",
                "Set code",
                "Collector number",
                "Foil",
                "Rarity",
                "Quantity",
                "Scryfall ID",
                "Purchase price",
                "Purchase price currency",
                "Condition",
                "Language",
                "Misprint",
                "Altered",
            ],
        )

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["Name"], self.normal_name)
        self.assertEqual(row["Set code"], "exp")
        self.assertEqual(row["Collector number"], "101")
        self.assertEqual(row["Foil"], "normal")
        self.assertEqual(row["Rarity"], "mythic")
        self.assertEqual(row["Quantity"], "2")
        self.assertEqual(row["Purchase price"], "2.50")
        self.assertEqual(row["Purchase price currency"], "USD")
        self.assertEqual(row["Condition"], "near_mint")
        self.assertEqual(row["Language"], "en")
        self.assertEqual(row["Misprint"], "false")
        self.assertEqual(row["Altered"], "false")

        expected_suffix = self.owner.replace(" ", "_")
        self.assertIn(
            f"manabox_export_{expected_suffix}.csv",
            response.headers["Content-Disposition"],
        )

    def test_both_exports_are_admin_only(self):
        for path in ("/export_csv", "/export_manabox_csv"):
            with self.subTest(path=path):
                self.assertEqual(self.admin_client.get(path).status_code, 200)
                self.assertEqual(self.vendor_client.get(path).status_code, 403)
                self.assertEqual(self.user_client.get(path).status_code, 403)
                self.assertEqual(self.guest_client.get(path).status_code, 302)


if __name__ == "__main__":
    unittest.main()
