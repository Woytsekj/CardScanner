"""Regression coverage for desktop options and session-authenticated mobile scanner APIs."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

_TEST_DATA = tempfile.mkdtemp(prefix="cardscanner-mobile-import-")
os.environ.setdefault("DATA_DIR", _TEST_DATA)
os.environ.setdefault("REQUIRE_LOGIN", "1")
os.environ.setdefault("SESSION_SECRET", "mobile-api-test-session-secret-0123456789")
os.environ.setdefault("SCANNER_API_KEY", "mobile-api-test-scanner-key-0123456789")
os.environ.setdefault("ADMIN_USERNAME", "ci-admin")
os.environ.setdefault("ADMIN_PASSWORD", "mobile-api-test-admin-password-0123456789")
os.environ.setdefault("TRUSTED_HOSTS", "localhost,127.0.0.1")
os.environ.setdefault("LOCAL_HOSTS", "localhost,127.0.0.1")
os.environ.setdefault("PUBLIC_HOSTNAME", "localhost")
os.environ.setdefault("PUBLIC_HTTPS", "0")

import app as app_module  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402


class ScannerMobileApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cardscanner-mobile-test-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        app_module.DB_INV = root / "mtg_inventory.db"
        app_module.DB_ART = root / "missing-art.db"
        app_module.PRICES_FILE = root / "tcg_prices.json"
        app_module.PRICE_META_FILE = root / "price_sync.json"
        app_module._PRICE_CACHE = {"key": None, "prices": {}}
        app_module._AUTH_READY = False
        app_module.migrate_db()
        self.client = app_module.app.test_client()
        self.csrf = "mobile-api-test-csrf-token-01234567890"
        with self.client.session_transaction() as session:
            session["username"] = "ci-admin"
            session["csrf_token"] = self.csrf
        conn = self.connect()
        self.me_id = conn.execute("SELECT id FROM people WHERE name='Me'").fetchone()[0]
        self.friend_id = conn.execute("SELECT id FROM people WHERE name='Friend'").fetchone()[0]
        conn.commit()
        conn.close()

    def connect(self):
        return app_module.connect_inventory()

    def api_key_headers(self):
        return {"X-Scanner-API-Key": app_module.SCANNER_API_KEY}

    def csrf_headers(self):
        return {"X-CSRFToken": self.csrf}

    def add_member(self, username="member1", person_name="Member Collection"):
        conn = self.connect()
        conn.execute("INSERT INTO people(name,active) VALUES(?,1)", (person_name,))
        person_id = conn.execute("SELECT id FROM people WHERE name=?", (person_name,)).fetchone()[0]
        conn.execute(
            "INSERT INTO users(username,password_hash,is_admin,role,is_superadmin,active,person_id) VALUES(?,?,?,?,?,?,?)",
            (username, generate_password_hash("unit-test-password-not-used"), 0, "member", 0, 1, person_id),
        )
        conn.commit()
        conn.close()
        return person_id

    def test_desktop_options_include_people_account_mapping_and_locations(self):
        member_id = self.add_member()
        conn = self.connect()
        conn.execute("INSERT INTO inventory(scryfall_id,name,owner,location,quantity,finish,for_vending) VALUES(?,?,?,?,?,?,?)",
                     ("sid-location", "Sample", "Member Collection", "Binder A", 1, "normal", 0))
        conn.commit(); conn.close()
        response = self.client.get("/api/scanner/options", headers={**self.api_key_headers(), **self.csrf_headers()}, base_url="http://localhost")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.get_json()
        member = next(p for p in data["people"] if p["id"] == member_id)
        self.assertEqual(member["account_id"], "member1")
        self.assertIn({"id": "Binder A", "name": "Binder A"}, data["locations"])
        self.assertIn({"id": "Free", "name": "Free"}, data["locations"])

    def test_desktop_scan_persists_location_vending_and_idempotency(self):
        payload = {
            "scan_id": "idempotent-mobile-test-1", "scryfall_id": "sid-test-1", "name": "Sol Ring",
            "set_code": "C21", "rarity": "uncommon", "collector_number": "123", "tcgplayer_id": "555",
            "price": "1.25", "owner": "Me", "person_id": self.me_id, "account_id": None,
            "quantity": 2, "finish": "normal", "location": "Binder A", "location_id": "Binder A",
            "is_vending": True,
        }
        first = self.client.post("/api/scan", json=payload, headers={**self.api_key_headers(), **self.csrf_headers()}, base_url="http://localhost")
        self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
        second = self.client.post("/api/scan", json=payload, headers={**self.api_key_headers(), **self.csrf_headers()}, base_url="http://localhost")
        self.assertEqual(second.status_code, 200, second.get_data(as_text=True))
        self.assertTrue(second.get_json().get("duplicate"))
        conn = self.connect()
        row = conn.execute("SELECT quantity,location,for_vending,deck_id FROM inventory WHERE scryfall_id='sid-test-1'").fetchone()
        conn.close()
        self.assertEqual(tuple(row), (2, "Binder A", 1, None))

    def test_wrong_account_mapping_is_rejected(self):
        payload = {"scryfall_id": "sid-wrong-map", "name": "Test", "owner": "Me",
                   "person_id": self.me_id, "account_id": "some-other-account", "tcgplayer_id": "555"}
        response = self.client.post("/api/scan", json=payload, headers={**self.api_key_headers(), **self.csrf_headers()}, base_url="http://localhost")
        self.assertEqual(response.status_code, 400)
        self.assertIn("mapping", response.get_json().get("error", "").lower())

    def test_mobile_options_scope_non_admin_to_linked_person(self):
        member_id = self.add_member()
        with self.client.session_transaction() as session:
            session["username"] = "member1"
            session["csrf_token"] = self.csrf
        response = self.client.get("/api/scanner/mobile/options", base_url="http://localhost")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.get_json()
        self.assertEqual([p["id"] for p in data["people"]], [member_id])
        self.assertEqual(data["people"][0]["account_id"], "member1")

    def test_mobile_scan_rejects_other_person_and_accepts_linked_person(self):
        member_id = self.add_member()
        with self.client.session_transaction() as session:
            session["username"] = "member1"
            session["csrf_token"] = self.csrf
        forbidden = {
            "scryfall_id": "sid-forbidden", "name": "Forbidden Card", "tcgplayer_id": "555",
            "owner": "Me", "person_id": self.me_id, "account_id": None, "quantity": 1,
            "location": "Free", "location_id": "Free", "finish": "normal", "is_vending": False,
        }
        response = self.client.post("/api/scanner/mobile/scan", json=forbidden, headers=self.csrf_headers(), base_url="http://localhost")
        self.assertEqual(response.status_code, 403, response.get_data(as_text=True))

        accepted = dict(forbidden, scryfall_id="sid-allowed", name="Allowed Card", owner="Member Collection",
                        person_id=member_id, account_id="member1", location="Binder A", location_id="Binder A",
                        is_vending=True, scan_id="member-scan-1")
        response = self.client.post("/api/scanner/mobile/scan", json=accepted, headers=self.csrf_headers(), base_url="http://localhost")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        conn = self.connect()
        row = conn.execute("SELECT owner,location,for_vending,quantity FROM inventory WHERE scryfall_id='sid-allowed'").fetchone()
        conn.close()
        self.assertEqual(tuple(row), ("Member Collection", "Binder A", 1, 1))

    def test_mobile_page_injects_csrf_token_and_camera_csp(self):
        response = self.client.get("/scanner/mobile/", base_url="http://localhost")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True)[:1000])
        html = response.get_data(as_text=True)
        self.assertIn(f'<meta name="csrf-token" content="{self.csrf}">', html)
        self.assertNotIn("__CARD_SCANNER_CSRF_TOKEN__", html)
        self.assertIn("camera=(self)", response.headers.get("Permissions-Policy", ""))
        self.assertIn("https://api.scryfall.com", response.headers.get("Content-Security-Policy", ""))
        self.assertEqual(response.headers.get("Cache-Control"), "private, no-store")


if __name__ == "__main__":
    unittest.main()
