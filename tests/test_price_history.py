"""Regression tests for MTG Scanner's TCGCSV price-history pipeline.

These tests use disposable SQLite databases and mocked TCGCSV responses. They
never access the production database or the real TCGCSV API.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import price_updater as updater


TCGCSV_MARKER = "2026-10-16T12:00:00Z"


class PriceHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="cardscanner-price-history-")
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.db_inv = self.root / "mtg_inventory.db"
        self.db_art = self.root / "mtg_art_hashes.db"
        self.prices_file = self.root / "tcg_prices.json"
        self.meta_file = self.root / "price_sync.json"
        self.last_seen_file = self.root / "tcgcsv_last_updated.txt"

        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        for name, value in {
            "DATA_DIR": self.root,
            "DB_INV": self.db_inv,
            "DB_ART": self.db_art,
            "PRICES_FILE": self.prices_file,
            "META_FILE": self.meta_file,
            "LAST_SEEN_FILE": self.last_seen_file,
            "PRICE_ALERT_ENABLED": False,
            "DISCORD_WEBHOOK_URL": "",
            "REQUEST_DELAY": 0,
        }.items():
            self.patches.enter_context(patch.object(updater, name, value))

        self._create_databases()

    def _create_databases(self) -> None:
        with sqlite3.connect(self.db_inv) as conn:
            conn.execute(
                """CREATE TABLE inventory (
                    id INTEGER PRIMARY KEY,
                    scryfall_id TEXT,
                    name TEXT NOT NULL,
                    set_code TEXT,
                    collector_number TEXT,
                    tcgplayer_id TEXT,
                    finish TEXT DEFAULT 'normal',
                    quantity INTEGER DEFAULT 1
                )"""
            )
            conn.executemany(
                """INSERT INTO inventory
                   (id, scryfall_id, name, set_code, collector_number,
                    tcgplayer_id, finish, quantity)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (1, "sid-direct", "Direct ID Card", "tst", "1", "1001", "normal", 1),
                    (2, "sid-art", "Art DB ID Card", "tst", "2", None, "foil", 1),
                    (3, "sid-mapped", "Mapped ID Card", "tst", "3", None, "normal", 2),
                ],
            )
            conn.execute(
                """CREATE TABLE card_price_mapping (
                    scryfall_id TEXT PRIMARY KEY,
                    tcgplayer_id TEXT,
                    finishes_json TEXT,
                    card_name TEXT,
                    set_code TEXT,
                    collector_number TEXT,
                    resolved_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                """INSERT INTO card_price_mapping
                   (scryfall_id, tcgplayer_id, finishes_json, card_name,
                    set_code, collector_number, resolved_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                ("sid-mapped", "3003", "[\"normal\"]", "Mapped ID Card", "tst", "3", TCGCSV_MARKER),
            )

        # Including scryfall_id in card_art also verifies that the SQL query
        # qualifies inventory columns instead of leaving them ambiguous.
        with sqlite3.connect(self.db_art) as conn:
            conn.execute(
                """CREATE TABLE card_art (
                    scryfall_id TEXT PRIMARY KEY,
                    tcgplayer_id TEXT,
                    collector_number TEXT
                )"""
            )
            conn.execute(
                "INSERT INTO card_art (scryfall_id, tcgplayer_id, collector_number) VALUES (?, ?, ?)",
                ("sid-art", "2002", "2"),
            )

    @staticmethod
    def sample_price_rows() -> list[dict[str, object]]:
        return [
            {"productId": 1001, "subTypeName": "Normal", "marketPrice": 1.25},
            {"productId": 2002, "subTypeName": "Normal", "marketPrice": 3.25},
            {"productId": 2002, "subTypeName": "Foil", "marketPrice": 7.50},
            {"productId": 3003, "subTypeName": "Normal", "marketPrice": 4.50},
        ]

    def test_current_price_map_does_not_guess_unknown_foil_subtypes(self) -> None:
        rows = [
            {"productId": 9001, "subTypeName": "Normal", "marketPrice": 1.00},
            {"productId": 9001, "subTypeName": "Foil", "marketPrice": 3.00},
            {"productId": 9001, "subTypeName": "Etched", "marketPrice": 4.00},
            {"productId": 9001, "subTypeName": "Reverse Holofoil", "marketPrice": 5.00},
        ]
        prices = updater.build_price_map(rows)["9001"]
        self.assertEqual(prices["normal"], 1.00)
        self.assertEqual(prices["foil"], 3.00)
        self.assertEqual(
            prices["subtypes"],
            {
                "Normal": 1.00,
                "Foil": 3.00,
                "Etched": 4.00,
                "Reverse Holofoil": 5.00,
            },
        )

    def test_records_history_using_inventory_art_database_and_mapping_ids(self) -> None:
        """All three supported TCGplayer-ID sources must produce history."""
        price_map = updater.build_price_map(self.sample_price_rows())

        history_rows, alerts_sent = updater.record_history_and_alerts(
            price_map, TCGCSV_MARKER
        )

        self.assertEqual(history_rows, 3)
        self.assertEqual(alerts_sent, 0)
        with sqlite3.connect(self.db_inv) as conn:
            rows = conn.execute(
                """SELECT tcgplayer_id, normal_price, foil_price
                   FROM price_history ORDER BY tcgplayer_id"""
            ).fetchall()

        self.assertEqual([row[0] for row in rows], ["1001", "2002", "3003"])
        by_id = {row[0]: (row[1], row[2]) for row in rows}
        self.assertEqual(by_id["1001"], (1.25, None))
        self.assertEqual(by_id["2002"], (3.25, 7.50))
        self.assertEqual(by_id["3003"], (4.50, None))

    def test_history_write_is_idempotent_for_the_same_tcgcsv_version(self) -> None:
        price_map = updater.build_price_map(self.sample_price_rows())

        first_count, _ = updater.record_history_and_alerts(price_map, TCGCSV_MARKER)
        second_count, _ = updater.record_history_and_alerts(price_map, TCGCSV_MARKER)

        self.assertEqual(first_count, 3)
        self.assertEqual(second_count, 0)
        with sqlite3.connect(self.db_inv) as conn:
            count = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
        self.assertEqual(count, 3)

    def test_unchanged_tcgcsv_marker_still_records_newly_tracked_cards(self) -> None:
        """A cached snapshot should fill missing history after ID mappings change."""
        self.prices_file.write_text(
            json.dumps({"success": True, "sourceUpdated": TCGCSV_MARKER, "results": self.sample_price_rows()}),
            encoding="utf-8",
        )
        self.last_seen_file.write_text(TCGCSV_MARKER + "\n", encoding="utf-8")
        self.meta_file.write_text(
            json.dumps({"source": "TCGCSV", "source_updated": TCGCSV_MARKER, "price_rows": 4}),
            encoding="utf-8",
        )

        with patch.object(updater, "get_last_updated", return_value=TCGCSV_MARKER), patch.object(
            updater,
            "get_json",
            side_effect=AssertionError("unchanged marker should use the cached price snapshot"),
        ):
            # False means no new TCGCSV snapshot was downloaded; history can
            # still be backfilled for cards that were not previously tracked.
            updated = updater.sync_prices(force=False)

        self.assertFalse(updated)
        with sqlite3.connect(self.db_inv) as conn:
            rows = conn.execute(
                "SELECT tcgplayer_id, source_updated FROM price_history ORDER BY tcgplayer_id"
            ).fetchall()
        self.assertEqual([row[0] for row in rows], ["1001", "2002", "3003"])
        self.assertTrue(all(row[1] == TCGCSV_MARKER for row in rows))
        meta = json.loads(self.meta_file.read_text(encoding="utf-8"))
        self.assertEqual(meta["history_rows_recorded"], 3)

    def test_empty_provider_feed_does_not_replace_snapshot_or_reconcile_history(self) -> None:
        previous_snapshot = {
            "success": True,
            "sourceUpdated": "previous-good-build",
            "results": self.sample_price_rows(),
        }
        previous_text = json.dumps(previous_snapshot, separators=(",", ":"))
        self.prices_file.write_text(previous_text, encoding="utf-8")
        self.last_seen_file.write_text("previous-good-build\n", encoding="utf-8")

        def fake_get_json(path: str) -> dict[str, object]:
            if path.endswith("/groups"):
                return {"success": True, "errors": [], "results": [{"groupId": 77}]}
            if path.endswith("/77/prices"):
                return {"success": True, "errors": [], "results": []}
            raise AssertionError(f"Unexpected TCGCSV path: {path}")

        with (
            patch.object(updater, "get_last_updated", return_value=TCGCSV_MARKER),
            patch.object(updater, "get_json", side_effect=fake_get_json),
            patch.object(updater.time, "sleep"),
            patch.object(
                updater,
                "record_history_and_alerts",
                side_effect=AssertionError("empty feed must not reconcile price history"),
            ),
        ):
            updated = updater.sync_prices(force=True)

        self.assertFalse(updated)
        self.assertEqual(self.prices_file.read_text(encoding="utf-8"), previous_text)
        self.assertEqual(
            self.last_seen_file.read_text(encoding="utf-8"),
            "previous-good-build\n",
        )
        meta = json.loads(self.meta_file.read_text(encoding="utf-8"))
        self.assertEqual(meta["price_rows"], 0)
        self.assertTrue(meta["failures"])
        self.assertIn("zero price rows", meta["failures"][-1]["error"])

    def test_failed_group_response_does_not_reconcile_partial_catalog(self) -> None:
        previous_snapshot = {
            "success": True,
            "sourceUpdated": "previous-good-build",
            "results": self.sample_price_rows(),
        }
        previous_text = json.dumps(previous_snapshot, separators=(",", ":"))
        self.prices_file.write_text(previous_text, encoding="utf-8")
        self.last_seen_file.write_text("previous-good-build\n", encoding="utf-8")

        def fake_get_json(path: str) -> dict[str, object]:
            if path.endswith("/groups"):
                return {
                    "success": True,
                    "errors": [],
                    "results": [{"groupId": 77}, {"groupId": 78}],
                }
            if path.endswith("/77/prices"):
                return {
                    "success": True,
                    "errors": [],
                    "results": self.sample_price_rows(),
                }
            if path.endswith("/78/prices"):
                return {
                    "success": False,
                    "errors": ["temporary provider error"],
                    "results": [],
                }
            raise AssertionError(f"Unexpected TCGCSV path: {path}")

        with (
            patch.object(updater, "get_last_updated", return_value=TCGCSV_MARKER),
            patch.object(updater, "get_json", side_effect=fake_get_json),
            patch.object(updater.time, "sleep"),
            patch.object(
                updater,
                "record_history_and_alerts",
                side_effect=AssertionError("partial feed must not reconcile price history"),
            ),
        ):
            updated = updater.sync_prices(force=True)

        self.assertFalse(updated)
        self.assertEqual(self.prices_file.read_text(encoding="utf-8"), previous_text)
        self.assertEqual(
            self.last_seen_file.read_text(encoding="utf-8"),
            "previous-good-build\n",
        )
        meta = json.loads(self.meta_file.read_text(encoding="utf-8"))
        self.assertEqual(meta["groups"], 2)
        self.assertEqual(meta["groups_synced"], 1)
        self.assertEqual(meta["price_rows"], len(self.sample_price_rows()))
        self.assertEqual(meta["failures"][0]["groupId"], 78)

    def test_unchanged_postgres_feed_skips_full_catalog_replay(self) -> None:
        snapshot = {
            "success": True,
            "sourceUpdated": TCGCSV_MARKER,
            "results": self.sample_price_rows(),
        }
        snapshot_text = json.dumps(snapshot, separators=(",", ":"))
        self.prices_file.write_text(snapshot_text, encoding="utf-8")
        self.last_seen_file.write_text(TCGCSV_MARKER + "\n", encoding="utf-8")
        self.meta_file.write_text(
            json.dumps({"source": "TCGCSV", "source_updated": TCGCSV_MARKER}),
            encoding="utf-8",
        )

        with (
            patch.object(updater, "DATABASE_BACKEND", "postgres"),
            patch.object(updater, "get_last_updated", return_value=TCGCSV_MARKER),
            patch.object(
                updater,
                "get_json",
                side_effect=AssertionError("unchanged PostgreSQL marker must not download feeds"),
            ),
            patch.object(
                updater,
                "record_history_and_alerts",
                side_effect=AssertionError("global PostgreSQL history must not be replayed"),
            ),
        ):
            updated = updater.sync_prices(force=False)

        self.assertFalse(updated)
        self.assertEqual(self.prices_file.read_text(encoding="utf-8"), snapshot_text)
        meta = json.loads(self.meta_file.read_text(encoding="utf-8"))
        self.assertEqual(meta["history_rows_recorded"], 0)
        self.assertEqual(meta["alerts_sent"], 0)
        self.assertEqual(meta["history_scope"], "full_provider_catalog")

    def test_forced_sync_downloads_mock_data_and_records_history(self) -> None:
        """Exercise the full sync path without a network call."""
        self.prices_file.write_text("{}", encoding="utf-8")
        self.last_seen_file.write_text(TCGCSV_MARKER + "\n", encoding="utf-8")

        def fake_get_json(path: str) -> dict[str, object]:
            if path.endswith("/groups"):
                return {"success": True, "errors": [], "results": [{"groupId": 77}]}
            if path.endswith("/77/prices"):
                return {"success": True, "errors": [], "results": self.sample_price_rows()}
            raise AssertionError(f"Unexpected TCGCSV path: {path}")

        with patch.object(updater, "get_last_updated", return_value=TCGCSV_MARKER), patch.object(
            updater, "get_json", side_effect=fake_get_json
        ), patch.object(updater.time, "sleep"):
            updated = updater.sync_prices(force=True)

        self.assertTrue(updated)
        with sqlite3.connect(self.db_inv) as conn:
            count = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
        self.assertEqual(count, 3)
        snapshot = json.loads(self.prices_file.read_text(encoding="utf-8"))
        self.assertEqual(len(snapshot["results"]), 4)
        meta = json.loads(self.meta_file.read_text(encoding="utf-8"))
        self.assertEqual(meta["history_rows_recorded"], 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
