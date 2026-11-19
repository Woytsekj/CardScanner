"""Unit tests for lossless SQLite-to-PostgreSQL migration helpers."""

from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from migrate_sqlite_backup import (
    GAME,
    SCRYFALL,
    TARGET_TABLES,
    rulings_cache_records,
)


class PostgreSQLMigrationRulingsTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            """
            CREATE TABLE card_rulings_cache (
                scryfall_id TEXT PRIMARY KEY,
                rulings_json TEXT NOT NULL DEFAULT '[]',
                fetched_at TEXT NOT NULL
            )
            """
        )

    def tearDown(self):
        self.db.close()

    def add_row(self, scryfall_id, rulings_json, fetched_at="2026-11-18 13:45:00"):
        self.db.execute(
            """
            INSERT INTO card_rulings_cache
                (scryfall_id, rulings_json, fetched_at)
            VALUES (?, ?, ?)
            """,
            (scryfall_id, rulings_json, fetched_at),
        )

    def test_cached_rulings_preserve_payload_timestamp_and_optional_printing_link(self):
        printing_id = "00000000-0000-4000-8000-000000000001"
        unresolved_id = "00000000-0000-4000-8000-000000000002"
        payload = [
            {
                "oracle_text": "A test ruling.",
                "source": "Wizards of the Coast",
                "published_at": "2024-01-02",
            }
        ]
        self.add_row(printing_id, json.dumps(payload))
        self.add_row(unresolved_id, "[]")

        rows = list(
            rulings_cache_records(
                self.db,
                {printing_id: 41},
            )
        )

        self.assertIn("catalog.printing_rulings_cache", TARGET_TABLES)
        self.assertEqual(len(rows), 2)

        resolved, unresolved = rows
        self.assertEqual(resolved[:4], (GAME, SCRYFALL, printing_id, 41))
        self.assertEqual(resolved[4].obj, payload)
        self.assertEqual(
            resolved[5],
            datetime(2026, 11, 18, 13, 45, tzinfo=timezone.utc),
        )

        self.assertEqual(
            unresolved[:4],
            (GAME, SCRYFALL, unresolved_id, None),
        )
        self.assertEqual(unresolved[4].obj, [])
        self.assertEqual(unresolved[5].utcoffset(), timedelta(0))

    def test_invalid_cached_rulings_json_is_rejected(self):
        self.add_row(
            "00000000-0000-4000-8000-000000000003",
            "{not-json}",
        )

        with self.assertRaisesRegex(RuntimeError, "Invalid JSON"):
            list(rulings_cache_records(self.db, {}))

    def test_blank_cached_printing_id_is_rejected(self):
        self.add_row("   ", "[]")

        with self.assertRaisesRegex(RuntimeError, "Blank Scryfall ID"):
            list(rulings_cache_records(self.db, {}))


if __name__ == "__main__":
    unittest.main()
