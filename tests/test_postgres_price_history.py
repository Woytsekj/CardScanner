"""PostgreSQL integration tests for compact TCGCSV price history."""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
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
            for table in (
                "catalog.provider_price_current",
                "catalog.provider_price_observations",
                "catalog.product_price_history",
                "app.price_alert_log",
            ):
                conn.execute(
                    f"""
                    DELETE FROM {table}
                    WHERE game_id = 'mtg'
                      AND provider_id = 'tcgcsv'
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

    def _normal_row(self, market=1.25, low=1.00, mid=1.10, high=1.50, direct=None):
        return {
            "productId": self.product_id,
            "subTypeName": "Normal",
            "marketPrice": market,
            "lowPrice": low,
            "midPrice": mid,
            "highPrice": high,
            "directLowPrice": direct,
        }

    def test_exact_subtypes_change_events_and_daily_reconstruction(self):
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
            self.assertFalse(has_owned_product(conn, self.product_id, vending_only=True))
            self.assertTrue(
                has_owned_product(conn, self.vending_product_id, vending_only=True)
            )

            captured = datetime.now(timezone.utc) - timedelta(days=4)
            source_rows = [
                self._normal_row(direct=0.95),
                {
                    "productId": self.product_id,
                    "subTypeName": "Foil",
                    "marketPrice": 4.50,
                    "lowPrice": 4.00,
                    "midPrice": 4.25,
                    "highPrice": 5.00,
                    "directLowPrice": 3.90,
                },
                {
                    "productId": self.product_id,
                    "subTypeName": "Reverse Holofoil",
                    "marketPrice": 5.25,
                },
            ]
            self.assertEqual(
                insert_price_observations(
                    conn, source_rows, "initial-build", captured
                ),
                3,
            )
            # A new source build with identical values updates last-seen state,
            # but does not create duplicate history events.
            self.assertEqual(
                insert_price_observations(
                    conn, source_rows, "unchanged-build",
                    captured + timedelta(days=1),
                ),
                0,
            )

            observations = conn.execute(
                """
                SELECT subtype_name, market_price, low_price, mid_price,
                       high_price, direct_low_price, is_available
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
            self.assertEqual(float(normal["mid_price"]), 1.10)
            self.assertEqual(float(normal["high_price"]), 1.50)
            self.assertEqual(float(normal["direct_low_price"]), 0.95)
            self.assertTrue(normal["is_available"])

            # One changed metric stores a complete new state for the subtype.
            changed_at = captured + timedelta(days=2)
            changed_rows = [
                self._normal_row(market=1.35, low=1.05, mid=1.15, high=1.60, direct=1.00),
                *source_rows[1:],
            ]
            self.assertEqual(
                insert_price_observations(
                    conn, changed_rows, "changed-build", changed_at
                ),
                1,
            )
            current = conn.execute(
                """
                SELECT market_price, low_price, mid_price, high_price,
                       direct_low_price, source_updated, is_available
                FROM catalog.provider_price_current
                WHERE game_id = 'mtg'
                  AND provider_id = 'tcgcsv'
                  AND external_product_id = ?
                  AND subtype_name = 'Normal'
                """,
                (self.product_id,),
            ).fetchone()
            self.assertEqual(float(current["market_price"]), 1.35)
            self.assertEqual(float(current["low_price"]), 1.05)
            self.assertEqual(float(current["mid_price"]), 1.15)
            self.assertEqual(float(current["high_price"]), 1.60)
            self.assertEqual(float(current["direct_low_price"]), 1.00)
            self.assertEqual(current["source_updated"], "changed-build")
            self.assertTrue(current["is_available"])

            # Missing from a complete feed writes an explicit unavailable event.
            self.assertEqual(
                insert_price_observations(
                    conn, [], "missing-build", changed_at + timedelta(days=1)
                ),
                3,
            )
            unavailable = conn.execute(
                """
                SELECT is_available, market_price
                FROM catalog.provider_price_current
                WHERE game_id = 'mtg'
                  AND provider_id = 'tcgcsv'
                  AND external_product_id = ?
                  AND subtype_name = 'Normal'
                """,
                (self.product_id,),
            ).fetchone()
            self.assertFalse(unavailable["is_available"])
            self.assertIsNone(unavailable["market_price"])

            # Reappearance with the same value is a new event after unavailability.
            self.assertEqual(
                insert_price_observations(
                    conn, changed_rows, "reappeared-build",
                    changed_at + timedelta(days=2),
                ),
                1,
            )
            reappeared = conn.execute(
                """
                SELECT is_available, market_price
                FROM catalog.provider_price_current
                WHERE game_id = 'mtg'
                  AND provider_id = 'tcgcsv'
                  AND external_product_id = ?
                  AND subtype_name = 'Normal'
                """,
                (self.product_id,),
            ).fetchone()
            self.assertTrue(reappeared["is_available"])
            self.assertEqual(float(reappeared["market_price"]), 1.35)

            series = fetch_price_history(conn, self.product_id, 365)
            normal_series = next(item for item in series if item["subtype_name"] == "Normal")
            self.assertGreaterEqual(len(normal_series["points"]), 365)
            # The final daily value is the reappeared state.
            self.assertEqual(normal_series["points"][-1]["market"], 1.35)
            self.assertTrue(normal_series["points"][-1]["available"])
            # The removal day is represented as unavailable, not a stale price.
            unavailable_days = [
                point for point in normal_series["points"]
                if point["captured_at"].startswith((changed_at + timedelta(days=1)).date().isoformat())
            ]
            self.assertTrue(unavailable_days)
            self.assertFalse(unavailable_days[-1]["available"])
            self.assertIsNone(unavailable_days[-1]["market"])

            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def test_retention_keeps_one_pre_window_baseline_per_subtype(self):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            now = datetime.now(timezone.utc)
            cutoff = now - timedelta(days=365)
            self.assertEqual(
                insert_price_observations(
                    conn, [self._normal_row(0.75)], "baseline-older",
                    now - timedelta(days=550),
                ),
                1,
            )
            self.assertEqual(
                insert_price_observations(
                    conn, [self._normal_row(1.00)], "baseline-newer",
                    now - timedelta(days=500),
                ),
                1,
            )
            self.assertEqual(
                insert_price_observations(
                    conn, [self._normal_row(1.25)], "inside-window", now
                ),
                1,
            )

            prune_price_history(
                conn, cutoff, product_ids=(self.product_id,)
            )
            remaining = conn.execute(
                """
                SELECT source_updated
                FROM catalog.provider_price_observations
                WHERE game_id = 'mtg'
                  AND provider_id = 'tcgcsv'
                  AND external_product_id = ?
                  AND subtype_name = 'Normal'
                ORDER BY captured_at
                """,
                (self.product_id,),
            ).fetchall()
            self.assertEqual(
                [row["source_updated"] for row in remaining],
                ["baseline-newer", "inside-window"],
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def test_updater_alert_uses_previous_event_before_new_build(self):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            old_time = datetime.now(timezone.utc) - timedelta(days=14)
            insert_price_observations(
                conn,
                [self._normal_row(1.00)],
                "old-alert-source-build",
                old_time,
            )
            conn.commit()
        finally:
            conn.close()

        source_rows = [{
            **self._normal_row(3.00, 2.75, 2.90, 3.25, None),
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

    def test_chart_api_uses_postgres_subtypes_and_vendor_vending_scope(self):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            now = datetime.now(timezone.utc)
            insert_price_observations(
                conn,
                [
                    self._normal_row(1.25),
                    {
                        "productId": self.vending_product_id,
                        "subTypeName": "Foil",
                        "marketPrice": 7.50,
                        "lowPrice": 7.00,
                        "midPrice": 7.25,
                        "highPrice": 8.00,
                        "directLowPrice": None,
                    },
                ],
                "chart-source-build",
                now,
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
        normal = next(
            item for item in admin_owned.get_json()["series"]
            if item["subtype_name"] == "Normal"
        )
        self.assertEqual(normal["points"][-1]["market"], 1.25)

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
        foil = next(
            item for item in vendor_visible.get_json()["series"]
            if item["subtype_name"] == "Foil"
        )
        self.assertEqual(foil["points"][-1]["market"], 7.50)

        missing = self.admin_client.get(
            f"/api/price-history/missing-{self.token}?days=90"
        )
        self.assertEqual(missing.status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
