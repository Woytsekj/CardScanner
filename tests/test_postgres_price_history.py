"""PostgreSQL integration tests for TCGCSV price history and chart access."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from postgres_app_db import connect_app_database
from postgres_db import close_pool, open_pool, run_migrations
import price_updater

from postgres_price_history import (
    fetch_owned_products,
    fetch_price_history,
    has_owned_product,
    insert_price_alert,
    insert_price_observations,
    insert_price_snapshot,
    latest_history_price,
    prune_price_history,
    recently_alerted,
)


@unittest.skipUnless(
    os.environ.get("CARD_SCANNER_POSTGRES_DSN")
    and os.environ.get("CARD_SCANNER_DATABASE_BACKEND") == "postgres",
    "Run with the PostgreSQL backend and development DSN configured",
)
class PostgreSQLPriceHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        open_pool()
        run_migrations()
        import app as app_module

        if app_module.DATABASE_BACKEND != "postgres":
            raise RuntimeError("Price-history tests require PostgreSQL app mode")
        cls.app_module = app_module
        cls.app_module.app.config["TESTING"] = True

    @classmethod
    def tearDownClass(cls):
        close_pool()

    def setUp(self):
        self.token = uuid4().hex
        self.prefix = f"PGPriceHistory{self.token}"
        self.owner = f"PG Price Owner {self.token}"
        self.admin_username = f"pg-price-admin-{self.token}"
        self.vendor_username = f"pg-price-vendor-{self.token}"
        self.product_id = f"price-{self.token}"
        self.vending_product_id = f"vending-price-{self.token}"
        self.csrf_token = f"csrf-price-{self.token}"

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

            conn.execute(
                """
                INSERT INTO app.users (
                    username, password_hash, is_admin, role,
                    is_superadmin, active, person_id
                )
                VALUES (?, ?, TRUE, 'admin', FALSE, TRUE, NULL)
                """,
                (self.admin_username, "unused-price-admin-password-hash"),
            )
            conn.execute(
                """
                INSERT INTO app.users (
                    username, password_hash, is_admin, role,
                    is_superadmin, active
                )
                VALUES (?, ?, FALSE, 'vendor', FALSE, TRUE)
                """,
                (self.vendor_username, "unused-price-vendor-password-hash"),
            )
            conn.execute(
                """
                INSERT INTO app.inventory (
                    game_id, name, owner, person_id, tcgplayer_id,
                    quantity, finish, for_vending
                )
                VALUES
                    ('mtg', ?, ?, ?, ?, 2, 'normal', FALSE),
                    ('mtg', ?, ?, ?, ?, 3, 'foil', TRUE)
                """,
                (
                    f"{self.prefix} Normal",
                    self.owner,
                    self.person_id,
                    self.product_id,
                    f"{self.prefix} Vending",
                    self.owner,
                    self.person_id,
                    self.vending_product_id,
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

    def make_client(self, username):
        client = self.app_module.app.test_client()
        with client.session_transaction() as session:
            session["username"] = username
            session["csrf_token"] = self.csrf_token
        return client

    def tearDown(self):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            product_ids = (self.product_id, self.vending_product_id)
            conn.execute(
                """
                DELETE FROM catalog.product_price_history
                WHERE game_id = 'mtg' AND provider_id = 'tcgcsv'
                  AND external_product_id IN (?, ?)
                """,
                product_ids,
            )
            conn.execute(
                """
                DELETE FROM catalog.provider_price_observations
                WHERE game_id = 'mtg' AND provider_id = 'tcgcsv'
                  AND external_product_id IN (?, ?)
                """,
                product_ids,
            )
            conn.execute(
                """
                DELETE FROM app.price_alert_log
                WHERE game_id = 'mtg' AND provider_id = 'tcgcsv'
                  AND external_product_id IN (?, ?)
                """,
                product_ids,
            )
            conn.execute(
                "DELETE FROM app.inventory WHERE name LIKE ?",
                (self.prefix + "%",),
            )
            conn.execute(
                "DELETE FROM app.users WHERE username IN (?, ?)",
                (self.admin_username, self.vendor_username),
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

    def test_owned_products_snapshots_and_exact_subtypes_are_preserved(self):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            fixture_product_ids = {self.product_id, self.vending_product_id}
            products = {
                row["tcgplayer_id"]: row
                for row in fetch_owned_products(conn)
                if row["tcgplayer_id"] in fixture_product_ids
            }
            self.assertEqual(set(products), fixture_product_ids)
            self.assertEqual(products[self.product_id]["normal_qty"], 2)
            self.assertEqual(products[self.product_id]["foil_qty"], 0)
            self.assertEqual(products[self.vending_product_id]["foil_qty"], 3)

            self.assertTrue(has_owned_product(conn, self.product_id))
            self.assertFalse(
                has_owned_product(conn, self.product_id, vending_only=True)
            )
            self.assertTrue(
                has_owned_product(conn, self.vending_product_id, vending_only=True)
            )

            captured = datetime.now(timezone.utc)
            old = captured - timedelta(days=10)
            self.assertTrue(
                insert_price_snapshot(
                    conn, self.product_id, "2026-11-08T12:00:00Z",
                    old, 1.25, 4.50,
                )
            )
            self.assertFalse(
                insert_price_snapshot(
                    conn, self.product_id, "2026-11-08T12:00:00Z",
                    captured, 1.25, 4.50,
                )
            )

            source_rows = [
                {
                    "productId": self.product_id,
                    "subTypeName": "Normal",
                    "marketPrice": 1.25,
                    "lowPrice": 1.00,
                    "midPrice": 1.10,
                    "highPrice": 1.50,
                    "directLowPrice": None,
                },
                {
                    "productId": self.product_id,
                    "subTypeName": "Foil",
                    "marketPrice": 4.50,
                },
                {
                    "productId": self.product_id,
                    "subTypeName": "Reverse Holofoil",
                    "marketPrice": 5.25,
                },
            ]
            self.assertEqual(
                insert_price_observations(
                    conn, source_rows, "2026-11-08T12:00:00Z", captured
                ),
                3,
            )
            self.assertEqual(
                insert_price_observations(
                    conn, source_rows, "2026-11-08T12:00:00Z", captured
                ),
                0,
            )

            observations = conn.execute(
                """
                SELECT subtype_name, market_price, low_price, direct_low_price
                FROM catalog.provider_price_observations
                WHERE game_id = 'mtg'
                  AND provider_id = 'tcgcsv'
                  AND external_product_id = ?
                ORDER BY subtype_name
                """,
                (self.product_id,),
            ).fetchall()
            self.assertEqual(
                [row["subtype_name"] for row in observations],
                ["Foil", "Normal", "Reverse Holofoil"],
            )
            normal = next(row for row in observations if row["subtype_name"] == "Normal")
            self.assertEqual(float(normal["market_price"]), 1.25)
            self.assertEqual(float(normal["low_price"]), 1.00)
            self.assertIsNone(normal["direct_low_price"])

            points = fetch_price_history(conn, self.product_id, 365)
            self.assertEqual(len(points), 1)
            self.assertEqual(points[0]["normal"], 1.25)
            self.assertEqual(points[0]["foil"], 4.50)
            self.assertEqual(points[0]["source_updated"], "2026-11-08T12:00:00Z")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def test_previous_price_alert_cooldown_and_retention(self):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            now = datetime.now(timezone.utc)
            old_capture = now - timedelta(days=14)
            retention_cutoff = now - timedelta(days=365)
            self.assertTrue(
                insert_price_snapshot(
                    conn, self.product_id, "old-source-build",
                    old_capture, 1.00, 2.00,
                )
            )

            prior = latest_history_price(
                conn, self.product_id, "normal", now - timedelta(days=1)
            )
            self.assertEqual(prior, 1.00)

            self.assertTrue(
                insert_price_snapshot(
                    conn, self.product_id, "current-source-build",
                    now, 3.00, 5.00,
                )
            )
            # A comparison made before the current insert must use the prior point.
            self.assertEqual(prior, 1.00)
            self.assertFalse(
                recently_alerted(
                    conn, self.product_id, "normal", now - timedelta(days=7)
                )
            )

            insert_price_alert(
                conn,
                tcg_id=self.product_id,
                finish="normal",
                alerted_at=now,
                old_price=1.00,
                new_price=3.00,
                percent_change=200.0,
                owned_quantity=2,
                owned_value_gain=4.00,
            )
            self.assertTrue(
                recently_alerted(
                    conn, self.product_id, "normal", now - timedelta(days=7)
                )
            )

            expired = now - timedelta(days=500)
            insert_price_snapshot(
                conn, self.vending_product_id, "expired-source-build",
                expired, 0.50, None,
            )
            prune_price_history(
                conn,
                retention_cutoff,
                product_ids=(self.product_id, self.vending_product_id),
            )

            remaining = conn.execute(
                """
                SELECT external_product_id, source_updated
                FROM catalog.product_price_history
                WHERE game_id = 'mtg'
                  AND provider_id = 'tcgcsv'
                  AND external_product_id IN (?, ?)
                ORDER BY external_product_id, source_updated
                """,
                (self.product_id, self.vending_product_id),
            ).fetchall()
            self.assertEqual(
                {
                    (row["external_product_id"], row["source_updated"])
                    for row in remaining
                },
                {
                    (self.product_id, "old-source-build"),
                    (self.product_id, "current-source-build"),
                },
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def test_updater_compares_against_previous_price_before_snapshot_insert(self):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            old_time = datetime.now(timezone.utc) - timedelta(days=14)
            insert_price_snapshot(
                conn, self.product_id, "old-alert-source-build",
                old_time, 1.00, None,
            )
            conn.commit()
        finally:
            conn.close()

        source_rows = [{
            "productId": self.product_id,
            "subTypeName": "Normal",
            "marketPrice": 3.00,
            "lowPrice": 2.75,
            "midPrice": 2.90,
            "highPrice": 3.25,
            "directLowPrice": None,
        }]
        with (
            patch.object(price_updater, "DATABASE_BACKEND", "postgres"),
            patch.object(price_updater, "PRICE_ALERT_ENABLED", True),
            patch.object(price_updater, "DISCORD_WEBHOOK_URL", "configured-for-test"),
            patch.object(price_updater, "PRICE_ALERT_LOOKBACK_DAYS", 30),
            patch.object(price_updater, "PRICE_ALERT_COOLDOWN_DAYS", 7),
            patch.object(price_updater, "PRICE_ALERT_MIN_DOLLARS", 1.00),
            patch.object(price_updater, "PRICE_ALERT_MIN_PERCENT", 25.0),
            patch.object(price_updater, "PRICE_ALERT_MIN_OWNED_VALUE_GAIN", 0.0),
            patch.object(
                price_updater,
                "send_discord_alert",
                return_value=(True, "sent"),
            ) as send_alert,
        ):
            history_count, alerts_sent = price_updater.record_history_and_alerts(
                {self.product_id: {"normal": 3.00, "foil": None}},
                "current-alert-source-build",
                source_rows,
            )

        self.assertEqual(history_count, 1)
        self.assertEqual(alerts_sent, 1)
        send_alert.assert_called_once()
        self.assertEqual(send_alert.call_args.args[2], 1.00)
        self.assertEqual(send_alert.call_args.args[3], 3.00)

        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            alert = conn.execute(
                """
                SELECT old_price, new_price, owned_quantity, owned_value_gain
                FROM app.price_alert_log
                WHERE external_product_id = ?
                  AND finish = 'normal'
                """,
                (self.product_id,),
            ).fetchone()
            self.assertIsNotNone(alert)
            self.assertEqual(float(alert["old_price"]), 1.00)
            self.assertEqual(float(alert["new_price"]), 3.00)
            self.assertEqual(alert["owned_quantity"], 2)
            self.assertEqual(float(alert["owned_value_gain"]), 4.00)
            conn.commit()
        finally:
            conn.close()

    def test_chart_api_uses_postgres_and_vendor_vending_scope(self):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            now = datetime.now(timezone.utc)
            insert_price_snapshot(
                conn, self.product_id, "chart-source-build", now, 1.25, None
            )
            insert_price_snapshot(
                conn, self.vending_product_id, "vending-chart-source-build",
                now, None, 7.50,
            )
            conn.commit()
        finally:
            conn.close()

        admin_owned = self.admin_client.get(
            f"/api/price-history/{self.product_id}?days=90"
        )
        self.assertEqual(
            admin_owned.status_code, 200, admin_owned.get_data(as_text=True)
        )
        self.assertEqual(admin_owned.get_json()["points"][0]["normal"], 1.25)

        vendor_hidden = self.vendor_client.get(
            f"/api/price-history/{self.product_id}?days=90"
        )
        self.assertEqual(vendor_hidden.status_code, 404)

        vendor_visible = self.vendor_client.get(
            f"/api/price-history/{self.vending_product_id}?days=90"
        )
        self.assertEqual(
            vendor_visible.status_code, 200, vendor_visible.get_data(as_text=True)
        )
        self.assertEqual(vendor_visible.get_json()["points"][0]["foil"], 7.50)

        missing = self.admin_client.get(
            f"/api/price-history/missing-{self.token}?days=90"
        )
        self.assertEqual(missing.status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
