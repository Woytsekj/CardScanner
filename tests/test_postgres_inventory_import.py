"""PostgreSQL integration tests for standard and ManaBox CSV imports."""

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
class PostgreSQLInventoryImportHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        open_pool()
        run_migrations()

        import app as app_module

        if app_module.DATABASE_BACKEND != "postgres":
            raise RuntimeError("Inventory import tests require PostgreSQL app mode")

        cls.app_module = app_module
        cls.app_module.app.config["TESTING"] = True

    @classmethod
    def tearDownClass(cls):
        close_pool()

    def setUp(self):
        self.token = uuid4().hex
        self.prefix = f"PGImport{self.token}"
        self.owner = f"PG Import Owner {self.token}"
        self.admin_username = f"pg-import-admin-{self.token}"
        self.vendor_username = f"pg-import-vendor-{self.token}"
        self.user_username = f"pg-import-user-{self.token}"
        self.user_owner = f"PG Import User Owner {self.token}"
        self.csrf_token = f"csrf-import-{self.token}"

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
            user_person = conn.execute(
                """
                INSERT INTO app.people (name, active)
                VALUES (?, TRUE)
                RETURNING id
                """,
                (self.user_owner,),
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
                (self.admin_username, "unused-import-test-password-hash", self.person_id),
            )
            conn.execute(
                """
                INSERT INTO app.users (
                    username, password_hash, is_admin, role,
                    is_superadmin, active
                )
                VALUES (?, ?, FALSE, 'vendor', FALSE, TRUE)
                """,
                (self.vendor_username, "unused-import-test-password-hash"),
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
                    "unused-import-test-password-hash",
                    self.user_person_id,
                    self.admin_username,
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
            session["csrf_token"] = self.csrf_token
        return client

    def make_csv(self, headers, rows):
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(headers)
        writer.writerows(rows)
        return output.getvalue().encode("utf-8")

    def upload(self, client, content, *, owner="", location="", csrf=True):
        data = {
            "file": (io.BytesIO(content), "collection.csv"),
            "owner": owner,
            "location": location,
        }
        headers = (
            {"X-CSRFToken": self.csrf_token}
            if csrf
            else {}
        )
        return client.post(
            "/import_csv",
            data=data,
            headers=headers,
            content_type="multipart/form-data",
        )

    def imported_rows(self):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            return conn.execute(
                """
                SELECT
                    id, scryfall_id, name, set_code, rarity, price,
                    scan_time, quantity, owner, person_id, finish,
                    collector_number, tcgplayer_id, location, for_vending,
                    card_id, printing_id
                FROM app.inventory
                WHERE name LIKE ?
                ORDER BY id
                """,
                (self.prefix + "%",),
            ).fetchall()
        finally:
            conn.close()

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
            conn.execute(
                "DELETE FROM app.people WHERE id IN (?, ?)",
                (self.person_id, self.user_person_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def test_standard_csv_import_preserves_fields_and_person_link(self):
        card_name = f"{self.prefix} Standard"
        csv_data = self.make_csv(
            [
                "scryfall_id", "name", "set_code", "rarity",
                "scanned_price", "finish", "quantity", "owner",
                "collector_number", "tcgplayer_id", "location",
                "for_vending", "scan_time",
            ],
            [[
                str(uuid4()), card_name, "exp", "mythic",
                "1.25", "normal", "3", self.owner.upper(),
                "007", "12345", "Box A", "yes",
                "2026-10-12T09:30:00-04:00",
            ]],
        )

        response = self.upload(self.admin_client, csv_data)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        result = response.get_json()
        self.assertTrue(result["success"])
        self.assertEqual(result["format"], "standard")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["skipped"], 0)

        rows = self.imported_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["name"], card_name)
        self.assertEqual(row["set_code"], "EXP")
        self.assertEqual(row["rarity"], "Mythic")
        self.assertEqual(row["price"], "1.25")
        self.assertEqual(row["quantity"], 3)
        self.assertEqual(row["owner"], self.owner)
        self.assertEqual(row["person_id"], self.person_id)
        self.assertEqual(row["finish"], "normal")
        self.assertEqual(row["collector_number"], "007")
        self.assertEqual(row["tcgplayer_id"], "12345")
        self.assertEqual(row["location"], "Box A")
        self.assertTrue(row["for_vending"])
        self.assertIsNotNone(row["scryfall_id"])
        self.assertIsNone(row["card_id"])
        self.assertIsNone(row["printing_id"])

    def test_manabox_foil_flag_overrides_finish_and_owner_location_overrides_work(self):
        normal_name = f"{self.prefix} ManaBox Normal"
        foil_name = f"{self.prefix} ManaBox Foil"
        csv_data = self.make_csv(
            [
                "Name", "Set code", "Collector number", "Foil",
                "Finish", "Rarity", "Quantity", "Scryfall ID",
                "Purchase price", "Purchase price currency", "Binder Name",
            ],
            [
                [
                    normal_name, "mh3", "012", "false", "foil",
                    "mythic", "3", "", "2.25", "USD", "ManaBox Binder",
                ],
                [
                    foil_name, "mh3", "013", "true", "normal",
                    "rare", "0", "", "3.50", "USD", "Second Binder",
                ],
            ],
        )

        response = self.upload(
            self.admin_client,
            csv_data,
            owner=self.owner,
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        result = response.get_json()
        self.assertTrue(result["success"])
        self.assertEqual(result["format"], "manabox")
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["skipped"], 0)

        rows = {row["name"]: row for row in self.imported_rows()}
        self.assertEqual(set(rows), {normal_name, foil_name})

        normal = rows[normal_name]
        self.assertEqual(normal["finish"], "normal")
        self.assertEqual(normal["owner"], self.owner)
        self.assertEqual(normal["person_id"], self.person_id)
        self.assertEqual(normal["location"], "ManaBox Binder")
        self.assertEqual(normal["set_code"], "MH3")
        self.assertEqual(normal["rarity"], "Mythic")
        self.assertEqual(normal["quantity"], 3)
        self.assertFalse(normal["for_vending"])
        self.assertIsNone(normal["scryfall_id"])

        foil = rows[foil_name]
        self.assertEqual(foil["finish"], "foil")
        self.assertEqual(foil["location"], "Second Binder")
        self.assertEqual(foil["rarity"], "Rare")
        self.assertEqual(foil["quantity"], 1)
        self.assertEqual(foil["price"], "3.50")

    def test_unknown_owner_and_blank_name_are_reported_as_skipped(self):
        valid_name = f"{self.prefix} Valid Owner"
        unknown_name = f"{self.prefix} Unknown Owner"
        csv_data = self.make_csv(
            ["name", "owner"],
            [
                [valid_name, self.owner],
                [unknown_name, "Not An Active Person"],
                ["", self.owner],
            ],
        )

        response = self.upload(self.admin_client, csv_data)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        result = response.get_json()
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["skipped"], 2)
        self.assertEqual(result["unknown_owners"], ["Not An Active Person"])
        rows = self.imported_rows()
        self.assertEqual({row["owner"] for row in rows}, {self.owner})
        self.assertEqual(
            result["note"],
            "Rows for unknown owners were skipped. Add those people in Admin, then re-import.",
        )
        self.assertEqual(
            [row["name"] for row in self.imported_rows()],
            [valid_name],
        )

    def test_invalid_timestamp_returns_400_without_importing_any_rows(self):
        csv_data = self.make_csv(
            ["name", "owner", "scan_time"],
            [
                [f"{self.prefix} Before Error", self.owner, "2026-11-01T12:00:00Z"],
                [f"{self.prefix} Invalid Date", self.owner, "not-a-date"],
            ],
        )

        response = self.upload(self.admin_client, csv_data)
        self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
        self.assertIn("Invalid scan date/time", response.get_json()["error"])
        self.assertEqual(self.imported_rows(), [])

    def test_database_failure_rolls_back_the_entire_csv(self):
        first_name = f"{self.prefix} Must Roll Back"
        second_name = f"{self.prefix} Invalid Quantity"
        csv_data = self.make_csv(
            ["name", "owner", "quantity"],
            [
                [first_name, self.owner, "2"],
                [second_name, self.owner, "999999999999999999999999999999"],
            ],
        )

        response = self.upload(self.admin_client, csv_data)
        self.assertEqual(response.status_code, 500, response.get_data(as_text=True))
        self.assertFalse(response.get_json()["success"])
        self.assertEqual(self.imported_rows(), [])

    def test_import_is_available_to_scoped_users_and_requires_csrf(self):
        csv_data = self.make_csv(
            ["name", "owner"],
            [[f"{self.prefix} Permissions", self.owner]],
        )
        user_csv = self.make_csv(
            ["name", "owner"],
            [[f"{self.prefix} User Permissions", self.user_owner]],
        )

        self.assertEqual(
            self.upload(self.vendor_client, csv_data).status_code,
            403,
        )
        user_response = self.upload(self.user_client, user_csv)
        self.assertEqual(user_response.status_code, 200, user_response.get_data(as_text=True))
        self.assertEqual(user_response.get_json()["count"], 1)
        self.assertEqual(
            {row["owner"] for row in self.imported_rows()},
            {self.user_owner},
        )
        self.assertEqual(
            self.upload(self.guest_client, csv_data).status_code,
            401,
        )

        response = self.upload(
            self.admin_client,
            csv_data,
            csrf=False,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("CSRF", response.get_data(as_text=True))
        self.assertEqual(self.imported_rows(), [])


if __name__ == "__main__":
    unittest.main()
