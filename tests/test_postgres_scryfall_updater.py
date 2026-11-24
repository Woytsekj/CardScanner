"""PostgreSQL Scryfall worker orchestration and durable status tests."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from postgres_db import close_pool, open_pool, run_migrations
from postgres_app_db import connect_app_database
import postgres_scryfall_updater as updater


@unittest.skipUnless(
    os.environ.get("CARD_SCANNER_POSTGRES_DSN"),
    "CARD_SCANNER_POSTGRES_DSN is not configured",
)
class PostgreSQLScryfallWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        open_pool()
        run_migrations()

    @classmethod
    def tearDownClass(cls):
        close_pool()

    def setUp(self):
        self.data_dir = tempfile.TemporaryDirectory(prefix="cardscanner-scryfall-test-")
        self.addCleanup(self.data_dir.cleanup)
        updater.DATA_DIR = Path(self.data_dir.name)

    def test_forced_sync_records_source_revision_and_import_counts(self):
        manifest = {
            "default_cards": {"updated_at": "2026-11-20T00:00:00Z"},
            "art_tags": {"updated_at": "2026-11-20T00:00:01Z"},
            "oracle_tags": {"updated_at": "2026-11-20T00:00:02Z"},
        }
        fake_counts = {
            "printings": 100, "hashed_images": 80, "tags": 25,
            "art_assignments": 40, "oracle_assignments": 30,
        }
        with (
            mock.patch.object(updater.http, "bulk_manifest", return_value=manifest),
            mock.patch.object(updater.http, "download_bulk", side_effect=["printings.gz", "art.gz", "oracle.gz"]),
            mock.patch.object(updater, "_catalog_counts", return_value=fake_counts),
            mock.patch.object(updater, "import_printings", return_value={"printings": 12, "with_images": 11}) as printing,
            mock.patch.object(updater, "import_tag_snapshots", return_value={"tags": 3, "aliases": 2, "art_assignments": 4, "oracle_assignments": 5}) as tags,
            mock.patch.object(updater, "fill_missing_hashes", return_value={"attempted": 0, "updated": 0, "failed": 0}) as hashes,
        ):
            result = updater.sync_once(force=True, skip_images=False)
        self.assertTrue(result["success"])
        self.assertEqual(printing.call_args.args[1], "printings.gz")
        self.assertEqual(tags.call_count, 1)
        self.assertEqual(hashes.call_count, 1)
        status = updater.fetch_sync_status()
        self.assertEqual(status["source_revision"]["default_cards"], manifest["default_cards"]["updated_at"])
        self.assertEqual(status["import_counts"]["printings"]["printings"], 12)
        self.assertIsNotNone(status["last_success_at"])
        self.assertIsNone(status["last_error"])

    def test_unchanged_sources_skip_bulk_download_and_import(self):
        manifest = {
            "default_cards": {"updated_at": "2026-11-22T00:00:00Z"},
            "art_tags": {"updated_at": "2026-11-22T00:00:01Z"},
            "oracle_tags": {"updated_at": "2026-11-22T00:00:02Z"},
        }
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            with conn:
                updater._write_status(
                    conn,
                    attempted_at=updater.utc_now(),
                    successful_at=updater.utc_now(),
                    source_revision={
                        key: value["updated_at"] for key, value in manifest.items()
                    },
                    import_counts={"prior": True},
                    error=None,
                )
        finally:
            conn.close()

        with (
            mock.patch.object(updater.http, "bulk_manifest", return_value=manifest),
            mock.patch.object(updater.http, "download_bulk") as download,
            mock.patch.object(updater, "import_printings") as printing,
            mock.patch.object(updater, "import_tag_snapshots") as tags,
            mock.patch.object(updater, "fill_missing_hashes") as hashes,
            mock.patch.object(updater, "_catalog_counts", return_value={
                "printings": 100, "hashed_images": 80, "tags": 25,
                "art_assignments": 40, "oracle_assignments": 30,
            }),
        ):
            result = updater.sync_once(skip_images=True)

        self.assertTrue(result["success"])
        download.assert_not_called()
        printing.assert_not_called()
        tags.assert_not_called()
        hashes.assert_not_called()

    def test_failed_snapshot_import_records_error_without_erasing_last_success(self):
        conn = connect_app_database()
        try:
            with conn:
                conn.execute(
                    """INSERT INTO catalog.provider_sync_state
                       (game_id,provider_id,last_success_at,source_revision,import_counts)
                       VALUES (%s,%s,now(),%s::jsonb,%s::jsonb)
                       ON CONFLICT (game_id,provider_id) DO UPDATE SET
                         last_success_at=EXCLUDED.last_success_at,
                         source_revision=EXCLUDED.source_revision,
                         import_counts=EXCLUDED.import_counts""",
                    ("mtg", "scryfall", '{"prior":"revision"}', '{"prior":1}'),
                )
        finally:
            conn.close()

        manifest = {
            "default_cards": {"updated_at": "2026-11-21T00:00:00Z"},
            "art_tags": {"updated_at": "2026-11-21T00:00:01Z"},
            "oracle_tags": {"updated_at": "2026-11-21T00:00:02Z"},
        }
        with (
            mock.patch.object(updater.http, "bulk_manifest", return_value=manifest),
            mock.patch.object(updater.http, "download_bulk", return_value="bad-printings.gz"),
            mock.patch.object(updater, "_catalog_counts", return_value={
                "printings": 100, "hashed_images": 80, "tags": 25,
                "art_assignments": 40, "oracle_assignments": 30,
            }),
            mock.patch.object(updater, "import_printings", side_effect=RuntimeError("interrupted bulk import")),
        ):
            with self.assertRaisesRegex(RuntimeError, "interrupted bulk import"):
                updater.sync_once(force=True, skip_images=True)
        status = updater.fetch_sync_status()
        self.assertIn("interrupted bulk import", status["last_error"])
        self.assertEqual(status["source_revision"], {"prior": "revision"})
        self.assertEqual(status["import_counts"], {"prior": 1})
        self.assertIsNotNone(status["last_success_at"])


if __name__ == "__main__":
    unittest.main()
