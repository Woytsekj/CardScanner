"""Unit tests for PostgreSQL backup creation, credentials and recovery safety."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest import mock

import postgres_backup


DSN = "postgresql://cardscanner:top%3Asecret%5Cpass@db.example.test:5432/cardscanner"


class PostgreSQLBackupTests(unittest.TestCase):
    def setUp(self):
        self.env_patch = mock.patch.dict(
            os.environ,
            {
                "CARD_SCANNER_POSTGRES_DSN": DSN,
                "CARD_SCANNER_POSTGRES_EXPECTED_DATABASE": "cardscanner",
            },
            clear=False,
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def test_uri_password_is_removed_and_pgpass_is_private_and_database_wildcarded(self):
        with postgres_backup._credentials(DSN) as (safe, env, database, user, password):
            self.assertNotIn("secret", safe)
            self.assertEqual((database, user), ("cardscanner", "cardscanner"))
            self.assertNotIn("PGPASSWORD", env)
            self.assertNotIn("CARD_SCANNER_POSTGRES_DSN", env)
            pgpass = Path(env["PGPASSFILE"])
            self.assertEqual(stat.S_IMODE(pgpass.stat().st_mode), 0o600)
            self.assertIn("db.example.test:5432:*:cardscanner:", pgpass.read_text())
            self.assertIn("top\\:secret\\\\pass", pgpass.read_text())

    def test_query_password_removed_but_sslmode_is_kept(self):
        dsn = "postgresql://cardscanner@db.example.test/cardscanner?password=query-secret&sslmode=require"
        with postgres_backup._credentials(dsn) as (safe, env, _db, _user, _password):
            self.assertNotIn("query-secret", safe)
            self.assertIn("sslmode=require", safe)
            self.assertNotIn("password=", safe)

    @mock.patch.dict(os.environ, {
        "CARD_SCANNER_POSTGRES_DSN": DSN,
        "CARD_SCANNER_POSTGRES_EXPECTED_DATABASE": "production_cardscanner",
    }, clear=False)
    def test_backup_refuses_unexpected_database_before_dump(self):
        with mock.patch.object(
            postgres_backup,
            "_run",
            side_effect=lambda args, env, password="": (
                CompletedProcess(args, 0, "cardscanner|cardscanner|17.5\n", "")
                if args[0] == "psql"
                else (_ for _ in ()).throw(AssertionError("pg_dump must not run"))
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "safety check failed"):
                with tempfile.TemporaryDirectory() as temp:
                    postgres_backup.create_backup(temp, timezone_name="UTC")

    @mock.patch.dict(os.environ, {"CARD_SCANNER_POSTGRES_DSN": DSN}, clear=False)
    def test_backup_writes_identity_size_sha_and_retains_latest_archives(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            for index in range(2):
                old = directory / f"cardscanner_postgres_old{index}.dump"
                old.write_bytes(b"old")
                old.with_suffix(old.suffix + ".json").write_text("{}")
                os.utime(old, (100 + index, 100 + index))

            def fake_run(args, env, password=""):
                if args[0] == "psql":
                    return CompletedProcess(args, 0, "cardscanner|cardscanner|17.5\n", "")
                if args[0] == "pg_dump" and "--version" in args:
                    return CompletedProcess(args, 0, "pg_dump (PostgreSQL) 17.5\n", "")
                if args[0] == "pg_dump":
                    Path(args[args.index("--file") + 1]).write_bytes(b"archive-fixture")
                    return CompletedProcess(args, 0, "", "")
                raise AssertionError(args)

            with mock.patch.object(postgres_backup, "_run", side_effect=fake_run):
                archive = postgres_backup.create_backup(directory, retention=2, timezone_name="UTC")
            info = json.loads(archive.with_suffix(archive.suffix + ".json").read_text())
            self.assertEqual(info["database_identity"]["database"], "cardscanner")
            self.assertEqual(info["archive_size_bytes"], archive.stat().st_size)
            self.assertEqual(info["sha256"], hashlib.sha256(archive.read_bytes()).hexdigest())
            self.assertEqual(len(list(directory.glob("cardscanner_postgres_*.dump"))), 2)

    @mock.patch.dict(os.environ, {"CARD_SCANNER_POSTGRES_DSN": DSN}, clear=False)
    def test_verify_creates_restores_checks_and_drops_scratch_database(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "fixture.dump"
            archive.write_bytes(b"archive-fixture")
            archive.with_suffix(".dump.json").write_text(json.dumps({
                "archive_size_bytes": archive.stat().st_size,
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "database_identity": {"database": "cardscanner"},
            }))
            commands = []

            def fake_run(args, env, password=""):
                commands.append(list(args))
                if args[0] == "psql" and "current_database()" in args[-1]:
                    return CompletedProcess(args, 0, "cardscanner|cardscanner|17.5\n", "")
                return CompletedProcess(args, 0, "|\n", "")

            with mock.patch.object(postgres_backup, "_run", side_effect=fake_run):
                result = postgres_backup.verify_backup(archive)
            self.assertTrue(result["verified"])
            self.assertTrue(result["temporary_database_removed"])
            self.assertEqual([c[0] for c in commands], ["psql", "createdb", "pg_restore", "psql", "dropdb"])
            self.assertIn("--single-transaction", commands[2])
            self.assertIn("--no-owner", commands[2])

    @mock.patch.dict(os.environ, {"CARD_SCANNER_POSTGRES_DSN": DSN}, clear=False)
    def test_checksum_mismatch_aborts_before_any_database_access(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "fixture.dump"
            archive.write_bytes(b"good")
            archive.with_suffix(".dump.json").write_text(json.dumps({
                "archive_size_bytes": 4, "sha256": hashlib.sha256(b"good").hexdigest(),
                "database_identity": {"database": "cardscanner"},
            }))
            archive.write_bytes(b"evil")
            with mock.patch.object(postgres_backup, "_run") as run:
                with self.assertRaisesRegex(RuntimeError, "SHA-256"):
                    postgres_backup.verify_backup(archive)
            run.assert_not_called()

    @mock.patch.dict(os.environ, {
        "CARD_SCANNER_POSTGRES_DSN": "postgresql://cardscanner:top%3Asecret%5Cpass@db.example.test:5432/postgres",
        "CARD_SCANNER_POSTGRES_EXPECTED_DATABASE": "postgres",
    }, clear=False)
    def test_restore_recovers_source_database_into_new_database_via_maintenance_connection(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "fixture.dump"
            archive.write_bytes(b"archive")
            archive.with_suffix(".dump.json").write_text(json.dumps({
                "archive_size_bytes": 7,
                "sha256": hashlib.sha256(b"archive").hexdigest(),
                "database_identity": {"database": "cardscanner"},
            }))
            commands = []

            def fake_run(args, env, password=""):
                commands.append(list(args))
                if args[0] == "psql" and "current_database()" in args[-1]:
                    return CompletedProcess(args, 0, "postgres|cardscanner|17.5\n", "")
                return CompletedProcess(args, 0, "|\n", "")

            with mock.patch.object(postgres_backup, "_run", side_effect=fake_run):
                result = postgres_backup.restore_backup(
                    archive,
                    "cardscanner_recovered",
                    create_database=True,
                )
            self.assertTrue(result["restored"])
            self.assertEqual(result["source_database"], "cardscanner")
            self.assertEqual(result["target_database"], "cardscanner_recovered")
            self.assertEqual(
                [command[0] for command in commands],
                ["createdb", "pg_restore", "psql"],
            )

    @mock.patch.dict(os.environ, {"CARD_SCANNER_POSTGRES_DSN": DSN}, clear=False)
    def test_restore_refuses_live_database_target(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "fixture.dump"
            archive.write_bytes(b"archive")
            archive.with_suffix(".dump.json").write_text(json.dumps({
                "archive_size_bytes": 7, "sha256": hashlib.sha256(b"archive").hexdigest(),
                "database_identity": {"database": "cardscanner"},
            }))
            with mock.patch.object(postgres_backup, "_run") as run:
                with self.assertRaisesRegex(RuntimeError, "Refusing to restore"):
                    postgres_backup.restore_backup(archive, "cardscanner")
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
