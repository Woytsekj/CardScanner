"""Regression coverage for desktop options and session-authenticated mobile scanner APIs."""
from __future__ import annotations

import os
import io
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
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
        app_module._SCANNER_MOBILE_NAMES_CACHE_KEY = None
        app_module._SCANNER_MOBILE_NAMES_CACHE = []
        app_module.migrate_db()
        # The auth bootstrap only creates an initial account when ADMIN_PASSWORD is
        # configured. Seed the known test account explicitly so authenticated mobile
        # endpoints are tested regardless of the developer's shell environment.
        conn = app_module.connect_inventory()
        conn.execute(
            "INSERT OR IGNORE INTO users(username,password_hash,is_admin,role,is_superadmin,active) VALUES(?,?,?,?,?,1)",
            ("ci-admin", generate_password_hash("mobile-api-test-admin-password-0123456789"), 1, "superadmin", 1),
        )
        conn.commit()
        conn.close()
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

    def test_mobile_recognize_requires_title_corroboration_for_unique_footer(self):
        import cv2
        import numpy as np

        art_path = Path(self.temp.name) / "mtg_art_hashes.db"
        app_module.DB_ART = art_path
        art = sqlite3.connect(art_path)
        art.execute("""CREATE TABLE card_art (
            scryfall_id TEXT PRIMARY KEY, name TEXT, set_code TEXT, set_name TEXT,
            rarity TEXT, tcgplayer_id TEXT, collector_number TEXT, phash TEXT
        )""")
        art.execute(
            "INSERT INTO card_art(scryfall_id,name,set_code,set_name,rarity,tcgplayer_id,collector_number,phash) VALUES(?,?,?,?,?,?,?,?)",
            ("sid-dedicated-eoe-175", "Dedicated Commons", "eoe", "Edge of Eternities", "common", "555", "175", "e"),
        )
        art.commit(); art.close()

        image = np.full((1120, 800, 3), 245, dtype=np.uint8)
        encoded_ok, encoded = cv2.imencode(".jpg", image)
        self.assertTrue(encoded_ok)

        class FooterReader:
            def __init__(self): self.calls = 0
            def readtext(self, image, detail=0, paragraph=False):
                self.calls += 1
                return ["E0E 0175 EN"] if self.calls == 1 else ["Dedicated Commons"]

        reader = FooterReader()
        with patch.object(app_module, "_scanner_mobile_get_easyocr_reader", return_value=reader):
            response = self.client.post(
                "/api/scanner/mobile/recognize",
                data={"card_image": (io.BytesIO(encoded.tobytes()), "card.jpg")},
                content_type="multipart/form-data",
                headers=self.csrf_headers(),
                base_url="http://localhost",
            )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.get_json()
        self.assertEqual(data["recognition_mode"], "metadata_title_verified")
        self.assertEqual(data["matched_name"], "Dedicated Commons")
        self.assertTrue(data["metadata_verified"])
        self.assertFalse(data["metadata_conflict"])
        self.assertEqual(data["ocr_passes"], 2)
        self.assertEqual(reader.calls, 2)

    def test_mobile_recognize_rejects_footer_match_that_conflicts_with_title(self):
        import cv2
        import numpy as np

        art_path = Path(self.temp.name) / "mtg_art_hashes.db"
        app_module.DB_ART = art_path
        art = sqlite3.connect(art_path)
        art.execute("""CREATE TABLE card_art (
            scryfall_id TEXT PRIMARY KEY, name TEXT, set_code TEXT, set_name TEXT,
            rarity TEXT, tcgplayer_id TEXT, collector_number TEXT, phash TEXT
        )""")
        art.executemany(
            "INSERT INTO card_art(scryfall_id,name,set_code,set_name,rarity,tcgplayer_id,collector_number,phash) VALUES(?,?,?,?,?,?,?,?)",
            [
                ("sid-wrong-footer", "Return to the Light Realms", "fra", "Reality Fracture", "rare", "555", "367", "a"),
                ("sid-visible-title", "Ajani's Anguish", "fra", "Reality Fracture", "rare", "556", "65", "b"),
            ],
        )
        art.commit(); art.close()
        app_module._SCANNER_MOBILE_SET_CODES_CACHE_KEY = None
        app_module._SCANNER_MOBILE_SET_CODES_CACHE = set()
        app_module._SCANNER_MOBILE_NAMES_CACHE_KEY = None
        app_module._SCANNER_MOBILE_NAMES_CACHE = []

        image = np.full((1120, 800, 3), 245, dtype=np.uint8)
        encoded_ok, encoded = cv2.imencode(".jpg", image)
        self.assertTrue(encoded_ok)

        class ConflictingReader:
            def __init__(self): self.calls = 0
            def readtext(self, _image, detail=0, paragraph=False):
                self.calls += 1
                return ["C 0367\nFRA EN"] if self.calls == 1 else ["Ajani's Anguish"]

        reader = ConflictingReader()
        with patch.object(app_module, "_scanner_mobile_get_easyocr_reader", return_value=reader):
            response = self.client.post(
                "/api/scanner/mobile/recognize",
                data={"card_image": (io.BytesIO(encoded.tobytes()), "card.jpg")},
                content_type="multipart/form-data",
                headers=self.csrf_headers(),
                base_url="http://localhost",
            )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.get_json()
        self.assertTrue(data["metadata_conflict"])
        self.assertFalse(data["metadata_verified"])
        self.assertEqual(data["matched_name"], "Ajani's Anguish")
        self.assertIsNone(data["detected_set_code"])
        self.assertIsNone(data["detected_collector_number"])
        self.assertEqual(reader.calls, 2)

    def test_footer_parser_requires_set_language_and_collector_row(self):
        art_path = Path(self.temp.name) / "mtg_art_hashes.db"
        app_module.DB_ART = art_path
        art = sqlite3.connect(art_path)
        art.execute("""CREATE TABLE card_art (
            scryfall_id TEXT PRIMARY KEY, name TEXT, set_code TEXT, set_name TEXT,
            rarity TEXT, tcgplayer_id TEXT, collector_number TEXT, phash TEXT
        )""")
        art.executemany(
            "INSERT INTO card_art(scryfall_id,name,set_code,set_name,rarity,tcgplayer_id,collector_number,phash) VALUES(?,?,?,?,?,?,?,?)",
            [
                ("sid-fra-65", "Screeching Soulbreaker", "fra", "Reality Fracture", "common", "716964", "65", "a"),
                ("sid-ren-1", "Example Card", "ren", "Renaissance", "rare", "1", "1", "b"),
            ],
        )
        art.commit(); art.close()
        app_module._SCANNER_MOBILE_SET_CODES_CACHE_KEY = None
        app_module._SCANNER_MOBILE_SET_CODES_CACHE = set()

        false_positive = app_module._scanner_mobile_parse_footer_metadata("REN 1")
        self.assertIsNone(false_positive["set_code"])
        self.assertIsNone(false_positive["collector_number"])

        parsed = app_module._scanner_mobile_parse_footer_metadata("C 0065\nFRA EN")
        self.assertEqual(parsed["set_code"], "fra")
        self.assertEqual(parsed["collector_number"], "65")

    def test_mobile_recognize_does_not_trust_unanchored_ren_one(self):
        import cv2
        import numpy as np

        art_path = Path(self.temp.name) / "mtg_art_hashes.db"
        app_module.DB_ART = art_path
        art = sqlite3.connect(art_path)
        art.execute("""CREATE TABLE card_art (
            scryfall_id TEXT PRIMARY KEY, name TEXT, set_code TEXT, set_name TEXT,
            rarity TEXT, tcgplayer_id TEXT, collector_number TEXT, phash TEXT
        )""")
        art.executemany(
            "INSERT INTO card_art(scryfall_id,name,set_code,set_name,rarity,tcgplayer_id,collector_number,phash) VALUES(?,?,?,?,?,?,?,?)",
            [
                ("sid-fra-65", "Screeching Soulbreaker", "fra", "Reality Fracture", "common", "716964", "65", "a"),
                ("sid-ren-1", "Example Card", "ren", "Renaissance", "rare", "1", "1", "b"),
            ],
        )
        art.commit(); art.close()
        app_module._SCANNER_MOBILE_SET_CODES_CACHE_KEY = None
        app_module._SCANNER_MOBILE_SET_CODES_CACHE = set()
        app_module._SCANNER_MOBILE_NAMES_CACHE_KEY = None
        app_module._SCANNER_MOBILE_NAMES_CACHE = []

        image = np.full((1120, 800, 3), 245, dtype=np.uint8)
        encoded_ok, encoded = cv2.imencode(".jpg", image)
        self.assertTrue(encoded_ok)

        class NoisyReader:
            def __init__(self): self.calls = 0
            def readtext(self, _image, detail=0, paragraph=False):
                self.calls += 1
                return ["REN 1"] if self.calls == 1 else ["Screeching Soulbreaker"]

        reader = NoisyReader()
        with patch.object(app_module, "_scanner_mobile_get_easyocr_reader", return_value=reader):
            response = self.client.post(
                "/api/scanner/mobile/recognize",
                data={"card_image": (io.BytesIO(encoded.tobytes()), "card.jpg")},
                content_type="multipart/form-data",
                headers=self.csrf_headers(),
                base_url="http://localhost",
            )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.get_json()
        self.assertEqual(data["recognition_mode"], "metadata_then_title")
        self.assertEqual(data["matched_name"], "Screeching Soulbreaker")
        self.assertNotEqual(data.get("detected_set_code"), "ren")
        self.assertEqual(reader.calls, 2)

    def test_mobile_search_uses_local_catalog_and_set_collector_without_scryfall(self):
        # The same SQLite catalog used by the desktop scanner provides printing choices.
        art_path = Path(self.temp.name) / "mtg_art_hashes.db"
        app_module.DB_ART = art_path
        art = sqlite3.connect(art_path)
        art.execute("""CREATE TABLE card_art (
            scryfall_id TEXT PRIMARY KEY, name TEXT, set_code TEXT, set_name TEXT,
            rarity TEXT, tcgplayer_id TEXT, collector_number TEXT, phash TEXT
        )""")
        art.executemany(
            "INSERT INTO card_art(scryfall_id,name,set_code,set_name,rarity,tcgplayer_id,collector_number,phash) VALUES(?,?,?,?,?,?,?,?)",
            [
                ("sid-sol-c21-123", "Sol Ring", "c21", "Commander 2021", "uncommon", "111", "123", "a"),
                ("sid-sol-2xm-254", "Sol Ring", "2xm", "Double Masters", "rare", "222", "254", "b"),
                ("sid-sol-c21-124", "Sol Ring", "c21", "Commander 2021", "uncommon", "333", "124", "c"),
                ("sid-unrelated", "Cardassia Prime", "trk", "Star Trek", "common", "444", "280", "d"),
                ("sid-dedicated-eoe-175", "Dedicated Commons", "eoe", "Edge of Eternities", "common", "555", "175", "e"),
            ],
        )
        art.commit()
        art.close()
        with patch.object(app_module.requests, "get") as outbound:
            response = self.client.get(
                "/api/scanner/mobile/search?name=Sol%20Ring&set_code=C21&collector_number=123",
                base_url="http://localhost",
            )
            outbound.assert_not_called()
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.get_json()
        self.assertFalse(data["network_lookup"])
        self.assertEqual(data["source"], "local_catalog")
        self.assertEqual(len(data["results"]), 1)
        self.assertEqual(data["results"][0]["scryfall_id"], "sid-sol-c21-123")
        self.assertEqual(data["results"][0]["set_name"], "Commander 2021")

        # Footer-only search may supply manual choices, but the mobile recognition
        # flow will only quick-confirm after the title corroborates a candidate.
        metadata_only = self.client.get(
            "/api/scanner/mobile/search?name=&metadata_text=E0E%200175%20EN",
            base_url="http://localhost",
        )
        self.assertEqual(metadata_only.status_code, 200, metadata_only.get_data(as_text=True))
        metadata_data = metadata_only.get_json()
        self.assertEqual(metadata_data["recognition_mode"], "metadata_first")
        self.assertEqual(metadata_data["matched_name"], "Dedicated Commons")
        self.assertEqual([row["scryfall_id"] for row in metadata_data["results"]], ["sid-dedicated-eoe-175"])

        # Mobile footer OCR should recover the set code and collector number locally,
        # normalize a leading-zero number, and correct a stray leading title glyph.
        with patch.object(app_module.requests, "get") as outbound:
            footer_response = self.client.get(
                "/api/scanner/mobile/search?name=8%20Dedicated%20Commons&metadata_text=L%200175%20E0E%20EN%20%26%20C%202026",
                base_url="http://localhost",
            )
            outbound.assert_not_called()
        self.assertEqual(footer_response.status_code, 200, footer_response.get_data(as_text=True))
        footer_data = footer_response.get_json()
        self.assertEqual(footer_data["detected_set_code"], "eoe")
        self.assertEqual(footer_data["detected_collector_number"], "175")
        self.assertEqual(footer_data["matched_name"], "Dedicated Commons")
        self.assertTrue(footer_data["ocr_name_corrected"])
        self.assertEqual([row["scryfall_id"] for row in footer_data["results"]], ["sid-dedicated-eoe-175"])
        self.assertFalse(footer_data["network_lookup"])

        # Even the optional mobile price endpoint must not fall back to a live API request.
        with patch.object(app_module.requests, "get") as outbound:
            price = self.client.get("/api/scanner/mobile/price/sid-sol-c21-123", base_url="http://localhost")
            outbound.assert_not_called()
        self.assertEqual(price.status_code, 200, price.get_data(as_text=True))
        self.assertEqual(price.get_json()["tcgplayer_id"], "111")

        no_match = self.client.get("/api/scanner/mobile/search?name=a%20p", base_url="http://localhost")
        self.assertEqual(no_match.status_code, 200)
        self.assertEqual(no_match.get_json()["results"], [])

    def test_mobile_search_corrects_common_ocr_typo_with_local_name_catalog(self):
        art_path = Path(self.temp.name) / "mtg_art_hashes.db"
        app_module.DB_ART = art_path
        art = sqlite3.connect(art_path)
        art.execute("""CREATE TABLE card_art (
            scryfall_id TEXT PRIMARY KEY, name TEXT, set_code TEXT, set_name TEXT,
            rarity TEXT, tcgplayer_id TEXT, collector_number TEXT, phash TEXT
        )""")
        art.executemany(
            "INSERT INTO card_art(scryfall_id,name,set_code,set_name,rarity,tcgplayer_id,collector_number,phash) VALUES(?,?,?,?,?,?,?,?)",
            [
                ("sid-soulbreaker", "Screeching Soulbreaker", "eoe", "Edge of Eternities", "rare", "777", "175", "a"),
                ("sid-soulbreaker-other", "Screeching Soulbreaker", "eoc", "Edge of Eternities Commander", "rare", "778", "21", "b"),
                ("sid-other", "Dedicated Commons", "eoe", "Edge of Eternities", "common", "779", "201", "c"),
            ],
        )
        art.commit(); art.close()
        app_module._SCANNER_MOBILE_NAMES_CACHE_KEY = None
        app_module._SCANNER_MOBILE_NAMES_CACHE = []
        with patch.object(app_module.requests, "get") as outbound:
            response = self.client.get(
                "/api/scanner/mobile/search?name=Screeching%20Souldbreaker&set_code=EOE&collector_number=175",
                base_url="http://localhost",
            )
            outbound.assert_not_called()
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.get_json()
        self.assertEqual(data["matched_name"], "Screeching Soulbreaker")
        self.assertEqual([row["scryfall_id"] for row in data["results"]], ["sid-soulbreaker"])
        self.assertFalse(data["network_lookup"])

    def test_mobile_search_can_recover_from_bad_title_using_exact_set_collector(self):
        art_path = Path(self.temp.name) / "mtg_art_hashes.db"
        app_module.DB_ART = art_path
        art = sqlite3.connect(art_path)
        art.execute("""CREATE TABLE card_art (
            scryfall_id TEXT PRIMARY KEY, name TEXT, set_code TEXT, set_name TEXT,
            rarity TEXT, tcgplayer_id TEXT, collector_number TEXT, phash TEXT
        )""")
        art.execute(
            "INSERT INTO card_art(scryfall_id,name,set_code,set_name,rarity,tcgplayer_id,collector_number,phash) VALUES(?,?,?,?,?,?,?,?)",
            ("sid-soulbreaker", "Screeching Soulbreaker", "eoe", "Edge of Eternities", "rare", "777", "175", "a"),
        )
        art.commit(); art.close()
        with patch.object(app_module.requests, "get") as outbound:
            response = self.client.get(
                "/api/scanner/mobile/search?name=&metadata_text=EOE%200175%20EN",
                base_url="http://localhost",
            )
            outbound.assert_not_called()
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.get_json()
        self.assertEqual(data["matched_name"], "Screeching Soulbreaker")
        self.assertEqual(data["detected_set_code"], "eoe")
        self.assertEqual(data["detected_collector_number"], "175")
        self.assertEqual([row["scryfall_id"] for row in data["results"]], ["sid-soulbreaker"])

    def test_mobile_fuzzy_title_recovery_can_find_a_clear_name_inside_noisy_ocr(self):
        with patch.object(app_module, "_scanner_mobile_cached_card_names", return_value=["Mountain", "Sol Ring", "Screeching Soulbreaker"]):
            ranked = app_module._scanner_mobile_fuzzy_card_names(
                "f RL pamm ll - Mountain y p - go EET TP SOY I Er os aa oo g gg ge oy 1 ' p - ite 8 HL 9 Zi al RY",
                limit=5,
            )
        self.assertTrue(ranked)
        self.assertEqual(ranked[0]["name"], "Mountain")
        self.assertTrue(ranked[0]["fragment_match"])
        self.assertTrue(ranked[0]["exact_fragment"])

    def test_mobile_recognize_offers_noisy_title_fragment_without_auto_match(self):
        from PIL import Image
        art_path = Path(self.temp.name) / "mtg_art_hashes.db"
        app_module.DB_ART = art_path
        art = sqlite3.connect(art_path)
        art.execute("""CREATE TABLE card_art (
            scryfall_id TEXT PRIMARY KEY, name TEXT, set_code TEXT, set_name TEXT,
            rarity TEXT, tcgplayer_id TEXT, collector_number TEXT, phash TEXT
        )""")
        art.executemany(
            "INSERT INTO card_art(scryfall_id,name,set_code,set_name,rarity,tcgplayer_id,collector_number,phash) VALUES(?,?,?,?,?,?,?,?)",
            [
                ("sid-mountain", "Mountain", "m21", "Core Set 2021", "basic", "1001", "277", "a"),
                ("sid-sol-ring", "Sol Ring", "c21", "Commander 2021", "uncommon", "1002", "267", "b"),
            ],
        )
        art.commit(); art.close()
        app_module._SCANNER_MOBILE_NAMES_CACHE_KEY = None
        app_module._SCANNER_MOBILE_NAMES_CACHE = []

        noisy_title = "f RL pamm ll - Mountain y p - go EET TP SOY I Er os aa oo g gg ge oy 1 ' p - ite 8 HL 9 Zi al RY"

        class FakeReader:
            def __init__(self): self.calls = 0
            def readtext(self, _image, detail=0, paragraph=False):
                self.calls += 1
                return [] if self.calls == 1 else [noisy_title]

        image_bytes = io.BytesIO()
        Image.new("RGB", (400, 560), "white").save(image_bytes, format="PNG")
        image_bytes.seek(0)
        reader = FakeReader()
        with patch.object(app_module, "_scanner_mobile_get_easyocr_reader", return_value=reader):
            response = self.client.post(
                "/api/scanner/mobile/recognize",
                data={"card_image": (image_bytes, "card.png")},
                headers=self.csrf_headers(),
                content_type="multipart/form-data",
                base_url="http://localhost",
            )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.get_json()
        self.assertIsNone(data["matched_name"], "Noisy OCR fragments must not independently verify a card title")
        self.assertEqual(data["fragment_suggestion"], "Mountain")
        self.assertEqual(data["fragment_score"], 100)
        self.assertFalse(data["metadata_verified"])
        self.assertEqual(reader.calls, 2)

    def test_mobile_local_easyocr_endpoint_uses_local_catalog_and_metadata(self):
        from PIL import Image
        art_path = Path(self.temp.name) / "mtg_art_hashes.db"
        app_module.DB_ART = art_path
        art = sqlite3.connect(art_path)
        art.execute("""CREATE TABLE card_art (
            scryfall_id TEXT PRIMARY KEY, name TEXT, set_code TEXT, set_name TEXT,
            rarity TEXT, tcgplayer_id TEXT, collector_number TEXT, phash TEXT
        )""")
        art.execute(
            "INSERT INTO card_art(scryfall_id,name,set_code,set_name,rarity,tcgplayer_id,collector_number,phash) VALUES(?,?,?,?,?,?,?,?)",
            ("sid-soulbreaker", "Screeching Soulbreaker", "eoe", "Edge of Eternities", "rare", "777", "175", "a"),
        )
        art.commit(); art.close()
        app_module._SCANNER_MOBILE_NAMES_CACHE_KEY = None
        app_module._SCANNER_MOBILE_NAMES_CACHE = []

        class FakeReader:
            def __init__(self): self.calls = 0
            def readtext(self, _image, detail=0, paragraph=False):
                self.calls += 1
                # Current recognition always corroborates footer metadata with the title.
                # Pass 1 is the lower-left footer; pass 2 is the card title.
                if self.calls == 1:
                    return ["EOE 0175 EN"]
                return ["Screeching Soulbreaker"]

        image_bytes = io.BytesIO()
        Image.new("RGB", (400, 560), "white").save(image_bytes, format="PNG")
        image_bytes.seek(0)
        fake_reader = FakeReader()
        with patch.object(app_module, "_scanner_mobile_get_easyocr_reader", return_value=fake_reader), \
             patch.object(app_module.requests, "get") as outbound:
            response = self.client.post(
                "/api/scanner/mobile/recognize",
                data={"card_image": (image_bytes, "card.png")},
                headers=self.csrf_headers(),
                content_type="multipart/form-data",
                base_url="http://localhost",
            )
            outbound.assert_not_called()
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.get_json()
        self.assertEqual(data["engine"], "local_easyocr")
        self.assertEqual(data["matched_name"], "Screeching Soulbreaker")
        self.assertFalse(data["network_lookup"])
        self.assertEqual(data["detected_set_code"], "eoe")
        self.assertEqual(data["detected_collector_number"], "175")
        self.assertEqual(data["recognition_mode"], "metadata_title_verified")
        self.assertEqual(data["ocr_passes"], 2)
        self.assertEqual(fake_reader.calls, 2)

    def test_mobile_page_injects_csrf_token_and_camera_csp(self):
        response = self.client.get("/scanner/mobile/", base_url="http://localhost")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True)[:1000])
        html = response.get_data(as_text=True)
        self.assertIn(f'<meta name="csrf-token" content="{self.csrf}">', html)
        self.assertIn("<title>CardScanner Scanner v39.11</title>", html)
        self.assertNotIn("__CARD_SCANNER_CSRF_TOKEN__", html)
        self.assertIn("camera=(self)", response.headers.get("Permissions-Policy", ""))
        self.assertIn("https://cdn.jsdelivr.net", response.headers.get("Content-Security-Policy", ""))
        self.assertNotIn("https://api.scryfall.com", response.headers.get("Content-Security-Policy", ""))
        self.assertIn("function detectCardBounds(bitmap)", html)
        self.assertIn("CardScanner reads the footer first:", html)
        self.assertIn("function recognizeCardMetadata(bitmap, rect)", html)
        self.assertIn("function getOcrWorker()", html)
        self.assertIn("function recognizeCardOnServer(bitmap, rect)", html)
        self.assertIn("function titleTextLooksNoisy(value)", html)
        self.assertIn("titleTextLooksNoisy(guess)", html)
        self.assertIn("localOCR?.fragment_suggestion", html)
        self.assertIn("/api/scanner/mobile/recognize", html)
        self.assertIn("const nativeZoomActive = state.hardwareZoom && state.zoom > 1.05", html)
        self.assertIn("metadata_text", html)
        self.assertNotIn('<h1>CardScanner Mobile</h1>', html)
        self.assertIn('id="status-title"', html)
        self.assertIn('id="status-detail"', html)
        self.assertIn('id="quick-confirm-bar"', html)
        self.assertIn('id="quick-confirm-yes"', html)
        self.assertIn('function showQuickConfirm(match)', html)
        self.assertIn('renderedMatches.length === 1 && options.allowQuickConfirm !== false', html)
        self.assertIn('selectPrinting(card, match.button)', html)
        self.assertIn('Use Save & next to add it to inventory.', html)

        self.assertIn('Scanner thinks: ${state.predictionName}', html)
        self.assertIn('renderedMatches.length === 1 && options.allowQuickConfirm !== false', html)
        self.assertIn('id="mobile-save-dock"', html)
        self.assertIn('id="save-card-sticky"', html)
        self.assertIn('id="vending-sticky"', html)
        self.assertIn("function syncVendingControls(source)", html)
        self.assertIn("every saved card is marked for vending", html)
        self.assertNotIn("$('vending').checked=false", html)
        self.assertIn("function syncSaveControls()", html)
        self.assertIn("const canSave = !!selected && state.people.length > 0 && personChosen && !state.saving", html)
        self.assertIn("$('person').addEventListener('change',syncSaveControls)", html)
        self.assertIn("scrollIntoView({behavior:'smooth', block:'start'})", html)
        self.assertEqual(response.headers.get("Cache-Control"), "private, no-store")

    def test_inventory_home_includes_mobile_and_desktop_scanner_links(self):
        response = self.client.get("/", base_url="http://localhost")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True)[:1000])
        html = response.get_data(as_text=True)
        self.assertRegex(html, r'href="/scanner/mobile/?"')
        self.assertIn('href="/scanner/desktop/download"', html)

    def test_inventory_database_has_collapsible_mobile_navigation_and_options(self):
        response = self.client.get("/", base_url="http://localhost")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True)[:1000])
        html = response.get_data(as_text=True)
        self.assertIn('id="siteNavMenu"', html)
        self.assertIn('aria-label="Site navigation"', html)
        self.assertIn('id="collectionSummary"', html)
        self.assertIn('id="inventoryOptions"', html)
        self.assertIn('Filters, sort &amp; display', html)
        self.assertIn("window.matchMedia('(min-width: 901px)')", html)
        self.assertIn('for (const menu of detailsMenus) menu.open = desktopLayout.matches;', html)
        options_start = html.index('id="inventoryOptions"')
        options_tag_start = html.rfind("<details", 0, options_start)
        options_tag_end = html.index(">", options_start) + 1
        options_tag = html[options_tag_start:options_tag_end]
        self.assertNotIn(" open", options_tag, "Inventory options should start collapsed on the database page")
        options_end = html.index("</details>", options_start)
        options_html = html[options_start:options_end]
        self.assertIn('class="toolbar-controls"', options_html)
        self.assertIn('class="filter-bar"', options_html)
        self.assertIn('name="vending"', options_html)
        self.assertNotIn("mobile_options", html, "Filter links should not force the mobile options panel open")

        # Old bookmarks/links may still carry this obsolete flag; it must not reopen the menu.
        legacy_response = self.client.get("/?mobile_options=1", base_url="http://localhost")
        self.assertEqual(legacy_response.status_code, 200, legacy_response.get_data(as_text=True)[:1000])
        legacy_html = legacy_response.get_data(as_text=True)
        legacy_options_start = legacy_html.index('id="inventoryOptions"')
        legacy_tag_start = legacy_html.rfind("<details", 0, legacy_options_start)
        legacy_tag_end = legacy_html.index(">", legacy_options_start) + 1
        self.assertNotIn(" open", legacy_html[legacy_tag_start:legacy_tag_end])

    def test_inventory_page_is_paginated_and_does_not_embed_full_autocomplete_index(self):
        conn = self.connect()
        conn.executemany(
            "INSERT INTO inventory(scryfall_id,name,set_code,rarity,price,owner,quantity,finish,location) VALUES(?,?,?,?,?,?,?,?,?)",
            [(f"perf-sid-{i:03d}", f"Performance Card {i:03d}", "TST", "common", f"{i / 100:.2f}", "Me", 1, "normal", "Free") for i in range(105)],
        )
        conn.commit(); conn.close()
        first = self.client.get("/?view=list&sort=name&dir=asc", base_url="http://localhost")
        self.assertEqual(first.status_code, 200, first.get_data(as_text=True)[:1000])
        html = first.get_data(as_text=True)
        self.assertIn("105 matching rows", html)
        self.assertIn("inventory-pagination", html)
        self.assertIn("Page 1 of 2", html)
        self.assertIn("Performance Card 000", html)
        self.assertNotIn("Performance Card 104", html)
        self.assertNotIn("const inventorySearchIndex =", html)
        second = self.client.get("/?view=list&sort=name&dir=asc&page=2", base_url="http://localhost")
        self.assertEqual(second.status_code, 200, second.get_data(as_text=True)[:1000])
        second_html = second.get_data(as_text=True)
        self.assertIn("Performance Card 104", second_html)
        self.assertNotIn("Performance Card 000", second_html)

        # Price sorts must also paginate in SQL; otherwise they used to load and
        # enrich every row before sorting just to show the first 100.
        price_first = self.client.get("/?view=list&sort=price&dir=desc", base_url="http://localhost")
        self.assertEqual(price_first.status_code, 200, price_first.get_data(as_text=True)[:1000])
        price_first_html = price_first.get_data(as_text=True)
        self.assertIn("Performance Card 104", price_first_html)
        self.assertNotIn("Performance Card 000", price_first_html)
        self.assertIn("Page 1 of 2", price_first_html)
        price_second = self.client.get("/?view=list&sort=price&dir=desc&page=2", base_url="http://localhost")
        self.assertEqual(price_second.status_code, 200, price_second.get_data(as_text=True)[:1000])
        price_second_html = price_second.get_data(as_text=True)
        self.assertIn("Performance Card 000", price_second_html)
        self.assertNotIn("Performance Card 104", price_second_html)

    def test_search_suggest_is_authenticated_and_can_scope_to_owner(self):
        self.add_member()
        conn = self.connect()
        conn.execute("INSERT INTO inventory(scryfall_id,name,set_code,rarity,price,owner,quantity,finish) VALUES(?,?,?,?,?,?,?,?)",
                     ("sid-suggest-me", "Mountain", "M21", "basic", "0.10", "Me", 2, "normal"))
        conn.execute("INSERT INTO inventory(scryfall_id,name,set_code,rarity,price,owner,quantity,finish) VALUES(?,?,?,?,?,?,?,?)",
                     ("sid-suggest-friend", "Mountain", "M20", "basic", "0.10", "Friend", 3, "normal"))
        conn.commit(); conn.close()
        guest = app_module.app.test_client()
        unauthenticated = guest.get("/search_suggest?q=Mountain", base_url="http://localhost")
        # The endpoint uses the same admin permission as the inventory page.
        self.assertIn(unauthenticated.status_code, (302, 401, 403))
        scoped = self.client.get("/search_suggest?q=Mountain&owner=Me", base_url="http://localhost")
        self.assertEqual(scoped.status_code, 200, scoped.get_data(as_text=True))
        results = scoped.get_json()["results"]
        self.assertTrue(results)
        self.assertTrue(all(row["quantity"] == 2 and row["set_code"] == "M21" for row in results))

    def test_inventory_card_has_direct_tcgplayer_product_link(self):
        conn = self.connect()
        conn.execute(
            """INSERT INTO inventory(scryfall_id,name,set_code,rarity,price,owner,quantity,finish,
                       tcgplayer_id,location,for_vending)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            ("sid-tcg-link", "Sol Ring", "C21", "Rare", "1.25", "Me", 1, "normal", "123456", "Free", 0),
        )
        conn.commit()
        conn.close()
        response = self.client.get("/?view=list", base_url="http://localhost")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True)[:1000])
        self.assertIn('href="https://www.tcgplayer.com/product/123456"', response.get_data(as_text=True))

    def test_desktop_download_contains_only_client_bundle_files(self):
        from zipfile import ZipFile
        response = self.client.get("/scanner/desktop/download", base_url="http://localhost")
        self.assertEqual(response.status_code, 200, f'Unexpected download response: status={response.status_code}, content_type={response.content_type}')
        self.assertIn("attachment", response.headers.get("Content-Disposition", "").lower())
        with ZipFile(io.BytesIO(response.data)) as archive:
            names = set(archive.namelist())
            self.assertIn("cardscanner-desktop/Scanner.py", names)
            self.assertIn("cardscanner-desktop/scanner_engine.py", names)
            self.assertIn("cardscanner-desktop/scanner.env.fish.example", names)
            self.assertNotIn("cardscanner-desktop/scanner.env.fish", names)
            self.assertFalse(any(name.endswith(".db") or name.endswith(".jsonl") for name in names))


if __name__ == "__main__":
    unittest.main()
