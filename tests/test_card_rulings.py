"""Regression tests for locally cached Scryfall card rulings."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

_TEST_DATA = tempfile.mkdtemp(prefix="cardscanner-rulings-import-")
os.environ.setdefault("DATA_DIR", _TEST_DATA)
os.environ.setdefault("REQUIRE_LOGIN", "1")
os.environ.setdefault("SESSION_SECRET", "rulings-test-session-secret-0123456789")
os.environ.setdefault("SCANNER_API_KEY", "rulings-test-scanner-key-0123456789")
os.environ.setdefault("ADMIN_USERNAME", "ci-admin")
os.environ.setdefault("ADMIN_PASSWORD", "rulings-test-admin-password-0123456789")
os.environ.setdefault("TRUSTED_HOSTS", "localhost,127.0.0.1")
os.environ.setdefault("LOCAL_HOSTS", "localhost,127.0.0.1")
os.environ.setdefault("PUBLIC_HOSTNAME", "localhost")
os.environ.setdefault("PUBLIC_HTTPS", "0")

import app as app_module  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402


CARD_ID = "550e8400-e29b-41d4-a716-446655440000"


class CardRulingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="cardscanner-rulings-test-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        app_module.DB_INV = root / "mtg_inventory.db"
        app_module.DB_ART = root / "mtg_art_hashes.db"
        app_module.PRICES_FILE = root / "tcg_prices.json"
        app_module.PRICE_META_FILE = root / "price_sync.json"
        app_module._AUTH_READY = False
        app_module.migrate_db()

        conn = app_module.connect_inventory()
        conn.execute(
            """INSERT OR IGNORE INTO users
               (username,password_hash,is_admin,role,is_superadmin,active)
               VALUES(?,?,?,?,?,1)""",
            ("ci-admin", generate_password_hash("rulings-test-admin-password-0123456789"),
             1, "superadmin", 1),
        )
        conn.commit()
        conn.close()

        self.client = app_module.app.test_client()
        with self.client.session_transaction() as session:
            session["username"] = "ci-admin"
            session["csrf_token"] = "rulings-test-csrf-token-0123456789"

    @staticmethod
    def scryfall_response(rows: list[dict[str, str]]) -> Mock:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"object": "list", "data": rows}
        return response

    def get_rulings(self):
        return self.client.get(f"/api/card-rulings/{CARD_ID}", base_url="http://localhost")

    def test_fetches_and_caches_rulings_then_uses_cache(self) -> None:
        rows = [
            {"published_at": "2019-06-14", "source": "wotc", "comment": "Older official ruling."},
            {"published_at": "2020-01-02", "source": "scryfall", "comment": "Newer Scryfall note."},
        ]
        response = self.scryfall_response(rows)
        with patch.object(app_module.requests, "get", return_value=response) as get:
            first = self.get_rulings()
            self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
            payload = first.get_json()
            self.assertEqual([r["published_at"] for r in payload["rulings"]], ["2020-01-02", "2019-06-14"])
            self.assertFalse(payload["cached"])

            second = self.get_rulings()
            self.assertEqual(second.status_code, 200, second.get_data(as_text=True))
            self.assertTrue(second.get_json()["cached"])
            self.assertEqual(second.get_json()["rulings"], payload["rulings"])
            get.assert_called_once()

        conn = app_module.connect_inventory()
        row = conn.execute(
            "SELECT rulings_json, fetched_at FROM card_rulings_cache WHERE scryfall_id=?",
            (CARD_ID,),
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertTrue(row["fetched_at"])
        self.assertEqual(len(__import__("json").loads(row["rulings_json"])), 2)

    def test_caches_empty_rulings_to_avoid_repeated_requests(self) -> None:
        response = self.scryfall_response([])
        with patch.object(app_module.requests, "get", return_value=response) as get:
            first = self.get_rulings()
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.get_json()["rulings"], [])
            second = self.get_rulings()
            self.assertEqual(second.status_code, 200)
            self.assertEqual(second.get_json()["rulings"], [])
            self.assertTrue(second.get_json()["cached"])
            get.assert_called_once()

    def test_returns_saved_rulings_when_refresh_fails(self) -> None:
        rows = [
            {"published_at": "2019-06-14", "source": "wotc", "comment": "Saved official ruling."},
        ]
        with patch.object(app_module.requests, "get", return_value=self.scryfall_response(rows)):
            initial = self.get_rulings()
        self.assertEqual(initial.status_code, 200)

        conn = app_module.connect_inventory()
        conn.execute(
            "UPDATE card_rulings_cache SET fetched_at='2020-01-01T00:00:00+00:00' WHERE scryfall_id=?",
            (CARD_ID,),
        )
        conn.commit()
        conn.close()

        with patch.object(app_module.requests, "get", side_effect=app_module.requests.Timeout("offline")):
            refreshed = self.get_rulings()
        self.assertEqual(refreshed.status_code, 200, refreshed.get_data(as_text=True))
        payload = refreshed.get_json()
        self.assertTrue(payload["stale"])
        self.assertIn("refreshing from Scryfall failed", payload["warning"])
        self.assertEqual(payload["rulings"][0]["comment"], "Saved official ruling.")

    def test_rejects_invalid_card_id_without_requesting_scryfall(self) -> None:
        with patch.object(app_module.requests, "get") as get:
            response = self.client.get("/api/card-rulings/not-a-card-id", base_url="http://localhost")
        self.assertEqual(response.status_code, 400)
        get.assert_not_called()

    def test_legality_labels_are_allowed_to_wrap_inside_grid_cells(self) -> None:
        root = Path(__file__).resolve().parents[1]
        css = (root / "static" / "app.css").read_text(encoding="utf-8")
        self.assertIn(".legality-chip {", css)
        self.assertIn("min-width:0", css)
        self.assertIn("overflow-wrap:anywhere", css)
        html = (root / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="oracleRulingsSection"', html)
        self.assertIn("loadOracleRulings", html)


if __name__ == "__main__":
    unittest.main()
