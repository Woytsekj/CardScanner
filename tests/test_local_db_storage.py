"""Tests for moving actively used SQLite files onto local storage."""
from __future__ import annotations

import sqlite3
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import backup_database
import migrate_storage


class LocalDatabaseStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="cardscanner-storage-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.nfs_data = self.root / "nfs-data"
        self.local_db = self.root / "local-db"
        self.nfs_backups = self.nfs_data / "backups"
        self.nfs_data.mkdir()
        self.local_db.mkdir()

    @staticmethod
    def create_db(path: Path, *, wal: bool = False) -> sqlite3.Connection:
        conn = sqlite3.connect(path)
        if wal:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO sample(value) VALUES('preserved row')")
        conn.commit()
        return conn

    def test_copy_databases_preserves_rows_and_does_not_delete_sources(self) -> None:
        inventory = self.nfs_data / "mtg_inventory.db"
        art = self.nfs_data / "mtg_art_hashes.db"
        self.create_db(inventory).close()
        self.create_db(art).close()

        with patch.multiple(
            migrate_storage,
            DATA_DIR=self.nfs_data,
            DB_DIR=self.local_db,
        ):
            copied = migrate_storage.copy_databases()

        self.assertEqual(copied, [self.local_db / "mtg_inventory.db", self.local_db / "mtg_art_hashes.db"])
        self.assertTrue(inventory.is_file())
        self.assertTrue(art.is_file())
        for path in copied:
            with sqlite3.connect(path) as conn:
                self.assertEqual(conn.execute("SELECT value FROM sample").fetchone()[0], "preserved row")
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_copy_uses_sqlite_backup_api_for_committed_wal_data(self) -> None:
        source = self.nfs_data / "source.db"
        destination = self.local_db / "destination.db"
        writer = self.create_db(source, wal=True)
        try:
            writer.execute("INSERT INTO sample(value) VALUES('committed WAL row')")
            writer.commit()
            migrate_storage.copy_database(source, destination)
        finally:
            writer.close()

        with sqlite3.connect(destination) as conn:
            values = [row[0] for row in conn.execute("SELECT value FROM sample ORDER BY id")]
        self.assertEqual(values, ["preserved row", "committed WAL row"])

    def test_migration_refuses_to_overwrite_existing_destination(self) -> None:
        source = self.nfs_data / "source.db"
        destination = self.local_db / "destination.db"
        self.create_db(source).close()
        destination.write_bytes(b"keep this file")

        with self.assertRaises(FileExistsError):
            migrate_storage.copy_database(source, destination)
        self.assertEqual(destination.read_bytes(), b"keep this file")

    def test_migration_requires_both_source_databases_before_copying(self) -> None:
        self.create_db(self.nfs_data / "mtg_inventory.db").close()
        with patch.multiple(migrate_storage, DATA_DIR=self.nfs_data, DB_DIR=self.local_db):
            with self.assertRaises(FileNotFoundError):
                migrate_storage.copy_databases()
        self.assertFalse((self.local_db / "mtg_inventory.db").exists())

    def test_backup_reads_local_databases_and_writes_archive_to_nas(self) -> None:
        local_inventory = self.local_db / "mtg_inventory.db"
        local_art = self.local_db / "mtg_art_hashes.db"
        self.create_db(local_inventory).close()
        self.create_db(local_art).close()
        with patch.multiple(
            backup_database,
            DATA_DIR=self.nfs_data,
            DB_DIR=self.local_db,
            BACKUP_DIR=self.nfs_backups,
            BACKUP_RETENTION=2,
        ):
            archive_path = backup_database.create_backup()

        self.assertIsNotNone(archive_path)
        self.assertTrue(str(archive_path).startswith(str(self.nfs_backups)))
        with tarfile.open(archive_path, "r:gz") as archive:
            names = set(archive.getnames())
            self.assertIn("mtg_inventory.db", names)
            self.assertIn("mtg_art_hashes.db", names)
            self.assertIn("backup_info.txt", names)

    def test_compose_mounts_local_db_for_every_service(self) -> None:
        root = Path(__file__).resolve().parents[1]
        compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertEqual(compose.count("${DB_HOST_DIR:-/var/lib/cardscanner/db}:/db"), 4)
        self.assertEqual(compose.count("DB_DIR: /db"), 4)

        for path in ("app.py", "price_updater.py", "scryfall_updater.py", "backup_database.py"):
            source = (root / path).read_text(encoding="utf-8")
            self.assertIn('os.environ.get("DB_DIR"', source, path)


if __name__ == "__main__":
    unittest.main()
