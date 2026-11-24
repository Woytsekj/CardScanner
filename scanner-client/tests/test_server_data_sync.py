"""Desktop scanner manifest/download integration tests for PostgreSQL exports."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_engine():
    imagehash = types.ModuleType("imagehash")
    imagehash.hex_to_hash = lambda value: value
    cv2 = types.ModuleType("cv2")
    numpy = types.ModuleType("numpy")
    easyocr = types.ModuleType("easyocr")
    easyocr.Reader = lambda *args, **kwargs: object()
    thefuzz = types.ModuleType("thefuzz")
    thefuzz.fuzz = types.SimpleNamespace()
    thefuzz.process = types.SimpleNamespace()
    with patch.dict(sys.modules, {
        "imagehash": imagehash,
        "cv2": cv2,
        "numpy": numpy,
        "easyocr": easyocr,
        "thefuzz": thefuzz,
    }):
        spec = importlib.util.spec_from_file_location(
            "scanner_engine_server_sync_test", ROOT / "scanner_engine.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, *, payload=None, content=b"", headers=None):
        self._payload = payload
        self._content = content
        self.headers = headers or {}
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload

    def iter_content(self, chunk_size=1024 * 1024):
        for start in range(0, len(self._content), max(1, chunk_size // 2)):
            yield self._content[start:start + max(1, chunk_size // 2)]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class ScannerServerSyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng = load_engine()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.eng.DB_NAME = str(self.root / "mtg_art_hashes.db")
        self.eng.PRICE_FILE = str(self.root / "tcg_prices.json")
        self.eng.SERVER_URL = "https://scanner-test.invalid"
        self.eng.SCANNER_API_KEY = "test-scanner-key"
        self.eng.cards_by_name.clear()
        self.eng.all_card_names.clear()
        self.eng.all_hashes_flat.clear()
        self.eng.tcg_prices.clear()
        self.art_bytes = self.make_artifact()
        self.price_bytes = json.dumps({
            "success": True,
            "sourceUpdated": "2026-11-23T12:00:00Z",
            "results": [{"productId": 123, "subTypeName": "Normal", "marketPrice": 1.25}],
        }).encode()

    def tearDown(self):
        self.temp.cleanup()

    def make_artifact(self):
        path = self.root / "server-art.db"
        conn = sqlite3.connect(path)
        try:
            conn.execute("""CREATE TABLE card_art (
                scryfall_id TEXT PRIMARY KEY, name TEXT, set_code TEXT, set_name TEXT,
                rarity TEXT, tcgplayer_id TEXT, collector_number TEXT, phash TEXT,
                oracle_id TEXT, illustration_id TEXT, image_uri TEXT, last_seen_build TEXT
            )""")
            conn.execute(
                """INSERT INTO card_art
                   (scryfall_id,name,set_code,rarity,tcgplayer_id,collector_number,phash)
                   VALUES(?,?,?,?,?,?,?)""",
                ("scryfall-sol-ring", "Sol Ring", "C21", "uncommon", "123", "1", "0123456789abcdef"),
            )
            conn.commit()
        finally:
            conn.close()
        return path.read_bytes()

    def manifest(self, *, art=None, prices=None):
        art = art if art is not None else self.art_bytes
        prices = prices if prices is not None else self.price_bytes
        return {
            "success": True,
            "server_time": "2026-11-24T14:00:00+00:00",
            "files": {
                "mtg_art_hashes.db": {
                    "available": True, "size": len(art),
                    "sha256": hashlib.sha256(art).hexdigest(),
                    "row_count": 1, "phash_count": 1,
                    "source_modified_at": "2026-11-23T12:00:00Z",
                },
                "tcg_prices.json": {
                    "available": True, "size": len(prices),
                    "sha256": hashlib.sha256(prices).hexdigest(),
                },
            },
        }

    def test_postgres_export_artifact_downloads_checks_and_reloads_in_memory(self):
        manifest = self.manifest()
        replies = [
            FakeResponse(payload=manifest),
            FakeResponse(content=self.art_bytes, headers={
                "X-Scanner-File-SHA256": manifest["files"]["mtg_art_hashes.db"]["sha256"]
            }),
            FakeResponse(content=self.price_bytes, headers={
                "X-Scanner-File-SHA256": manifest["files"]["tcg_prices.json"]["sha256"]
            }),
        ]
        with patch.object(self.eng.requests, "get", side_effect=replies) as get:
            result = self.eng.sync_server_data()

        self.assertTrue(result["success"], result)
        self.assertEqual(result["downloaded"], ["mtg_art_hashes.db", "tcg_prices.json"])
        self.assertEqual(get.call_count, 3)
        self.assertTrue(self.eng.load_local_data())
        self.assertEqual(self.eng.all_hashes_flat[0]["id"], "scryfall-sol-ring")
        self.assertEqual(self.eng.cards_by_name["sol ring"][0]["set"], "C21")
        self.assertEqual(self.eng.tcg_prices["123"]["normal"], 1.25)
        self.assertFalse(Path(self.eng.DB_NAME + ".download").exists())
        self.assertFalse(Path(self.eng.PRICE_FILE + ".download").exists())

    def test_checksum_failure_never_replaces_existing_local_catalog(self):
        old_path = Path(self.eng.DB_NAME)
        old_path.write_bytes(b"working-local-catalog")
        manifest = self.manifest()
        wrong_sha = "0" * 64
        manifest["files"]["mtg_art_hashes.db"]["sha256"] = wrong_sha
        replies = [
            FakeResponse(payload=manifest),
            FakeResponse(content=self.art_bytes, headers={"X-Scanner-File-SHA256": wrong_sha}),
            FakeResponse(content=self.price_bytes),
        ]
        with patch.object(self.eng.requests, "get", side_effect=replies):
            result = self.eng.sync_server_data()
        self.assertFalse(result["success"])
        self.assertEqual(old_path.read_bytes(), b"working-local-catalog")
        self.assertFalse(Path(self.eng.DB_NAME + ".download").exists())
        self.assertIn("SHA256 verification failed", " ".join(result["errors"]))

    def test_current_manifest_does_not_redownload_existing_files(self):
        Path(self.eng.DB_NAME).write_bytes(self.art_bytes)
        Path(self.eng.PRICE_FILE).write_bytes(self.price_bytes)
        manifest = self.manifest()
        with patch.object(
            self.eng.requests, "get",
            return_value=FakeResponse(payload=manifest),
        ) as get:
            result = self.eng.sync_server_data()
        self.assertTrue(result["success"], result)
        self.assertEqual(set(result["skipped"]), {"mtg_art_hashes.db", "tcg_prices.json"})
        self.assertEqual(get.call_count, 1)


if __name__ == "__main__":
    unittest.main()
