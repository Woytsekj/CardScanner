"""Regression tests for scanner-client inventory metadata and retry behavior.

The scanner's OCR stack is heavyweight and initializes at import time. Stub the
three import-time-only OCR/hash helpers so these tests exercise the metadata and
SQLite/HTTP error paths without loading model weights.
"""
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_engine():
    imagehash = types.ModuleType("imagehash")
    easyocr = types.ModuleType("easyocr")
    easyocr.Reader = lambda *args, **kwargs: object()
    thefuzz = types.ModuleType("thefuzz")
    thefuzz.fuzz = types.SimpleNamespace()
    thefuzz.process = types.SimpleNamespace()
    with patch.dict(sys.modules, {"imagehash": imagehash, "easyocr": easyocr, "thefuzz": thefuzz}):
        spec = importlib.util.spec_from_file_location("scanner_engine_metadata_test", ROOT / "scanner_engine.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class ScannerMetadataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng = load_engine()

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.eng.INV_DB_NAME = str(root / "inventory.sqlite3")
        self.eng.QUEUE_FILE = str(root / "scan_queue.jsonl")
        self.eng.REJECTED_QUEUE_FILE = str(root / "scan_queue_rejected.jsonl")
        self.eng.SERVER_URL = "https://scanner-test.invalid"
        self.eng.SCANNER_API_KEY = "test-only-key"
        self.match = {
            "id": "scryfall-123", "name": "Sol Ring", "set": "C21",
            "rarity": "uncommon", "collector_number": "123", "tcg_id": "555",
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_person_account_and_location_normalization(self):
        people = self.eng._normalize_person_options([
            {"id": 5, "name": "Andrew", "account_id": 2, "account_name": "andrew"},
            {"id": 6, "name": "Guest", "account_id": 3, "account_name": "guest"},
            "Legacy Person",
        ])
        self.assertEqual([p["name"] for p in people], ["Andrew", "Guest", "Legacy Person"])
        self.assertEqual(people[0]["account_id"], 2)
        locations = self.eng._normalize_location_options(["Binder", {"id": 4, "name": "Long Box"}, {"id": 4, "name": "Long Box"}])
        self.assertEqual(len(locations), 2)
        self.assertEqual(locations[1]["id"], 4)

    def test_local_inventory_quantity_dedupes_by_location_and_vending(self):
        first = self.eng.save_to_local_inventory(self.match, "1.25", "Andrew", "normal", 2,
            person_id=5, account_id=2, location="Binder", is_vending=False)
        second = self.eng.save_to_local_inventory(self.match, "1.25", "Andrew", "normal", 1,
            person_id=5, account_id=2, location="Binder", is_vending=False)
        other_location = self.eng.save_to_local_inventory(self.match, "1.25", "Andrew", "normal", 1,
            person_id=5, account_id=2, location="Long Box", is_vending=False)
        vending = self.eng.save_to_local_inventory(self.match, "1.25", "Andrew", "normal", 1,
            person_id=5, account_id=2, location="Binder", is_vending=True)
        self.assertEqual((first["quantity"], second["quantity"], other_location["quantity"], vending["quantity"]), (2, 3, 1, 1))

    def test_remote_payload_contains_account_location_and_vending_fields(self):
        captured = {}
        def accept(payload):
            captured.update(payload)
            return {"success": True, "quantity": 2}
        with patch.object(self.eng, "post_remote_scan", side_effect=accept):
            result = self.eng.save_scan(self.match, "1.25", "Andrew", "normal", 2,
                person_id=5, account_id=2, location="Binder", location_id=7, is_vending=True)
        self.assertEqual(result["mode"], "server")
        self.assertEqual(captured["person_id"], 5)
        self.assertEqual(captured["account_id"], 2)
        self.assertEqual(captured["location"], "Binder")
        self.assertEqual(captured["location_id"], 7)
        self.assertTrue(captured["is_vending"])
        self.assertTrue(captured["vending"])

    def test_http_409_validation_failure_is_not_queued(self):
        response = self.eng.requests.Response()
        response.status_code = 409
        response._content = b'{"success":false,"error":"Deck conflict"}'
        http_error = self.eng.requests.HTTPError("409 conflict", response=response)
        with patch.object(self.eng, "post_remote_scan", side_effect=http_error), \
             patch.object(self.eng, "queue_scan", side_effect=AssertionError("must not queue a 409")):
            result = self.eng.save_scan(self.match, "1.25", "Andrew", "normal", is_vending=True)
        self.assertFalse(result["success"])
        self.assertEqual(result["mode"], "rejected")
        self.assertIn("Deck conflict", result["error"])

    def test_immediate_validation_rejection_is_not_queued(self):
        with patch.object(self.eng, "post_remote_scan", side_effect=self.eng.ScannerAPIRejected("Deck-assigned card cannot be vending")), \
             patch.object(self.eng, "queue_scan", side_effect=AssertionError("must not queue a rejected scan")):
            result = self.eng.save_scan(self.match, "1.25", "Andrew", "normal", person_id=5, account_id=2,
                location="Binder", is_vending=True)
        self.assertFalse(result["success"])
        self.assertEqual(result["mode"], "rejected")

    def test_queued_validation_rejection_is_preserved_and_does_not_block_queue(self):
        payload = {"name": "Sol Ring", "is_vending": True}
        Path(self.eng.QUEUE_FILE).write_text(json.dumps(payload) + "\n", encoding="utf-8")
        with patch.object(self.eng, "post_remote_scan", side_effect=self.eng.ScannerAPIRejected("Deck conflict")):
            self.eng.flush_pending_queue()
        rejected_path = Path(self.eng.REJECTED_QUEUE_FILE)
        self.assertTrue(rejected_path.exists())
        record = json.loads(rejected_path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(record["payload"], payload)
        self.assertIn("Deck conflict", record["error"])
        self.assertFalse(Path(self.eng.QUEUE_FILE).exists())


if __name__ == "__main__":
    unittest.main()
