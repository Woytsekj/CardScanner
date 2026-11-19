"""PostgreSQL-mode route tests for file-only scanner sync and Discord diagnostics."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch
from uuid import uuid4

from postgres_app_db import connect_app_database
from postgres_db import close_pool, open_pool, run_migrations


@unittest.skipUnless(
    os.environ.get("CARD_SCANNER_POSTGRES_DSN")
    and os.environ.get("CARD_SCANNER_DATABASE_BACKEND") == "postgres",
    "Run with the PostgreSQL backend and development DSN configured",
)
class PostgreSQLExternalIntegrationRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        open_pool()
        run_migrations()

        import app as app_module

        if app_module.DATABASE_BACKEND != "postgres":
            raise RuntimeError("External integration tests require PostgreSQL app mode")
        cls.app_module = app_module
        cls.app_module.app.config["TESTING"] = True

    @classmethod
    def tearDownClass(cls):
        close_pool()

    def setUp(self):
        self.token = uuid4().hex
        self.username = f"pg-integration-admin-{self.token}"
        self.csrf = f"csrf-integration-{self.token}"
        self.scanner_key = f"scanner-integration-key-{self.token}"

        self.temp_dir = tempfile.TemporaryDirectory(prefix="cardscanner-pg-integrations-")
        self.data_dir = Path(self.temp_dir.name)
        self.art_path = self.data_dir / "mtg_art_hashes.db"
        self.prices_path = self.data_dir / "tcg_prices.json"
        self.price_payload = b'{"123456":{"normal":2.5,"foil":4.75}}'
        self.prices_path.write_bytes(self.price_payload)
        self._create_art_catalog()

        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            conn.execute(
                """
                INSERT INTO app.users (
                    username, password_hash, is_admin, role,
                    is_superadmin, active
                )
                VALUES (?, ?, TRUE, 'admin', FALSE, TRUE)
                """,
                (self.username, "unused-integration-test-password-hash"),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        self.client = self.app_module.app.test_client()
        with self.client.session_transaction() as session:
            session["username"] = self.username
            session["csrf_token"] = self.csrf

        self.patchers = [
            patch.object(self.app_module, "SCANNER_API_KEY", self.scanner_key),
            patch.object(self.app_module, "DATA_DIR", self.data_dir),
            patch.object(self.app_module, "DB_ART", self.art_path),
            patch.object(self.app_module, "PRICES_FILE", self.prices_path),
            patch.object(
                self.app_module,
                "get_price_meta",
                return_value={"source_updated": "integration-test-source"},
            ),
        ]
        for patcher in self.patchers:
            patcher.start()

        self._reset_art_snapshot_cache()

    def _create_art_catalog(self):
        with sqlite3.connect(self.art_path) as conn:
            conn.execute(
                """
                CREATE TABLE card_art (
                    scryfall_id TEXT,
                    name TEXT,
                    set_code TEXT,
                    rarity TEXT,
                    tcgplayer_id TEXT,
                    phash TEXT,
                    collector_number TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO card_art (
                    scryfall_id, name, set_code, rarity,
                    tcgplayer_id, phash, collector_number
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    "Integration Test Card",
                    "tst",
                    "rare",
                    "123456",
                    "0123456789abcdef",
                    "1",
                ),
            )

    def _reset_art_snapshot_cache(self):
        self.app_module._SCANNER_CLIENT_ART_SNAPSHOT_SIGNATURE = None
        self.app_module._SCANNER_CLIENT_ART_SNAPSHOT_PATH = None
        self.app_module._SCANNER_CLIENT_ART_SNAPSHOT_INFO = {}

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self._reset_art_snapshot_cache()

        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            conn.execute(
                "DELETE FROM app.users WHERE username = ?",
                (self.username,),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        self.temp_dir.cleanup()

    def scanner_headers(self, key=None):
        return {"X-Scanner-API-Key": self.scanner_key if key is None else key}

    def test_scanner_manifest_requires_api_key(self):
        missing = self.client.get(
            "/api/scanner/client-data/manifest",
            base_url="http://localhost",
        )
        self.assertEqual(missing.status_code, 401)

        invalid = self.client.get(
            "/api/scanner/client-data/manifest",
            headers=self.scanner_headers("incorrect-key"),
            base_url="http://localhost",
        )
        self.assertEqual(invalid.status_code, 401)

    def test_scanner_manifest_reports_art_and_price_file_metadata(self):
        response = self.client.get(
            "/api/scanner/client-data/manifest",
            headers=self.scanner_headers(),
            base_url="http://localhost",
        )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["price_source_updated"], "integration-test-source")

        art = payload["files"]["mtg_art_hashes.db"]
        self.assertTrue(art["available"])
        self.assertEqual(art["row_count"], 1)
        self.assertEqual(art["phash_count"], 1)
        self.assertGreater(art["size"], 0)
        self.assertEqual(len(art["sha256"]), 64)

        prices = payload["files"]["tcg_prices.json"]
        self.assertTrue(prices["available"])
        self.assertEqual(prices["size"], len(self.price_payload))
        self.assertEqual(prices["sha256"], hashlib.sha256(self.price_payload).hexdigest())
        self.assertEqual(prices["source_updated"], "integration-test-source")

    def test_scanner_price_download_returns_sha256_and_private_cache_headers(self):
        response = self.client.get(
            "/api/scanner/client-data/tcg_prices.json",
            headers=self.scanner_headers(),
            base_url="http://localhost",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, self.price_payload)
        self.assertEqual(
            response.headers.get("X-Scanner-File-SHA256"),
            hashlib.sha256(self.price_payload).hexdigest(),
        )
        self.assertIn("no-store", response.headers.get("Cache-Control", ""))

    def test_scanner_art_download_is_a_valid_sqlite_snapshot(self):
        response = self.client.get(
            "/api/scanner/client-data/mtg_art_hashes.db",
            headers=self.scanner_headers(),
            base_url="http://localhost",
        )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(
            response.headers.get("X-Scanner-File-SHA256"),
            hashlib.sha256(response.data).hexdigest(),
        )

        downloaded = self.data_dir / "downloaded-art.db"
        downloaded.write_bytes(response.data)
        with sqlite3.connect(downloaded) as conn:
            self.assertEqual(
                conn.execute("PRAGMA integrity_check").fetchone()[0],
                "ok",
            )
            row = conn.execute(
                "SELECT name, phash FROM card_art"
            ).fetchone()
        self.assertEqual(row, ("Integration Test Card", "0123456789abcdef"))

    def test_scanner_download_rejects_unknown_file_names(self):
        response = self.client.get(
            "/api/scanner/client-data/not-allow-listed.db",
            headers=self.scanner_headers(),
            base_url="http://localhost",
        )

        self.assertEqual(response.status_code, 404)
        self.assertIn("Unsupported scanner data file", response.get_json()["error"])

    def test_discord_test_posts_with_admin_session_and_csrf(self):
        with patch.object(
            self.app_module,
            "send_discord_message",
            return_value=(True, ""),
        ) as send_message:
            response = self.client.post(
                "/admin/test_discord",
                data={"csrf_token": self.csrf},
                base_url="http://localhost",
            )

        self.assertEqual(response.status_code, 302)
        message = parse_qs(urlparse(response.headers["Location"]).query)["message"][0]
        self.assertEqual(message, "Discord test sent.")
        send_message.assert_called_once()
        payload = send_message.call_args.args[0]
        self.assertEqual(
            payload["embeds"][0]["title"],
            "MTG Scanner Inventory — Discord test",
        )

    def test_discord_test_reports_webhook_failure(self):
        with patch.object(
            self.app_module,
            "send_discord_message",
            return_value=(False, "Webhook is not configured"),
        ) as send_message:
            response = self.client.post(
                "/admin/test_discord",
                data={"csrf_token": self.csrf},
                base_url="http://localhost",
            )

        self.assertEqual(response.status_code, 302)
        message = parse_qs(urlparse(response.headers["Location"]).query)["message"][0]
        self.assertEqual(message, "Discord test failed: Webhook is not configured")
        send_message.assert_called_once()

    def test_discord_test_rejects_invalid_csrf_before_sending(self):
        with patch.object(
            self.app_module,
            "send_discord_message",
            return_value=(True, ""),
        ) as send_message:
            response = self.client.post(
                "/admin/test_discord",
                data={"csrf_token": "wrong-token"},
                base_url="http://localhost",
            )

        self.assertEqual(response.status_code, 400)
        send_message.assert_not_called()

    def test_discord_test_requires_admin_login(self):
        guest = self.app_module.app.test_client()
        response = guest.post(
            "/admin/test_discord",
            data={"csrf_token": "not-a-session-token"},
            base_url="http://localhost",
        )
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
