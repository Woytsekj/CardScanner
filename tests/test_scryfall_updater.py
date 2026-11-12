"""Regression tests for the Scryfall printing/hash/Tagger catalog updater."""

from __future__ import annotations

import gzip
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import scryfall_updater as updater
from scryfall_catalog import ensure_art_catalog_schema


class ScryfallUpdaterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "mtg_art_hashes.db"
        self.data_dir_patch = mock.patch.object(updater, "DATA_DIR", self.root)
        self.db_patch = mock.patch.object(updater, "DB_ART", self.db)
        self.meta_patch = mock.patch.object(updater, "META_FILE", self.root / "sync.json")
        self.data_dir_patch.start()
        self.db_patch.start()
        self.meta_patch.start()
        ensure_art_catalog_schema(self.db)

    def tearDown(self):
        self.meta_patch.stop()
        self.db_patch.stop()
        self.data_dir_patch.stop()
        self.temp.cleanup()

    def write_jsonl(self, name, records):
        path = self.root / name
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
        return path

    def connect(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        return conn

    def test_printing_import_adds_metadata_and_face_rows_without_losing_legacy_hash(self):
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO card_art (scryfall_id,name,phash) VALUES(?,?,?)",
                ("legacy-id", "Legacy Card", "0123456789abcdef"),
            )

        source = self.write_jsonl("printings.jsonl.gz", [
            {
                "id": "legacy-id",
                "name": "Legacy Card",
                "set": "tst",
                "set_name": "Test Set",
                "rarity": "rare",
                "tcgplayer_id": 123,
                "collector_number": "7",
                "oracle_id": "oracle-1",
                "illustration_id": "illustration-1",
                "image_uris": {"normal": "https://cards.scryfall.io/normal/front/test.jpg"},
            },
            {
                "id": "new-id",
                "name": "Two-Face Test",
                "set": "tst",
                "collector_number": "8",
                "oracle_id": "oracle-2",
                "card_faces": [
                    {
                        "oracle_id": "oracle-2",
                        "illustration_id": "front-art",
                        "image_uris": {"normal": "https://cards.scryfall.io/front.jpg"},
                    },
                    {
                        "oracle_id": "oracle-2",
                        "illustration_id": "back-art",
                        "image_uris": {"normal": "https://cards.scryfall.io/back.jpg"},
                    },
                ],
            },
        ])

        stats = updater.import_printings(source, "2026-11-12T10:00:00Z")
        self.assertEqual(stats["printings"], 2)
        with self.connect() as conn:
            legacy = conn.execute(
                "SELECT phash,image_uri,oracle_id FROM card_art WHERE scryfall_id='legacy-id'"
            ).fetchone()
            faces = conn.execute(
                "SELECT face_index,illustration_id FROM card_art_faces WHERE scryfall_id='new-id' ORDER BY face_index"
            ).fetchall()
        self.assertEqual(legacy["phash"], "0123456789abcdef")
        self.assertEqual(legacy["image_uri"], "https://cards.scryfall.io/normal/front/test.jpg")
        self.assertEqual(legacy["oracle_id"], "oracle-1")
        self.assertEqual([(row["face_index"], row["illustration_id"]) for row in faces],
                         [(0, "front-art"), (1, "back-art")])

    def test_tag_import_indexes_art_oracle_aliases_and_hierarchy(self):
        art = self.write_jsonl("art.jsonl.gz", [{
            "id": "art-tag-id",
            "slug": "floating-island",
            "label": "Floating island",
            "parent_ids": ["parent-id"],
            "aliases": ["sky island"],
            "taggings": [{"illustration_id": "illustration-1", "weight": "high"}],
        }])
        oracle = self.write_jsonl("oracle.jsonl.gz", [{
            "id": "oracle-tag-id",
            "slug": "ramp",
            "label": "Ramp",
            "aliases": ["mana acceleration"],
            "taggings": [{"oracle_id": "oracle-1", "weight": "median"}],
        }])

        stats = updater.import_tag_snapshots(art, oracle, {"art_tags": "a", "oracle_tags": "o"})
        self.assertEqual(stats["art_assignments"], 1)
        self.assertEqual(stats["oracle_assignments"], 1)
        with self.connect() as conn:
            art_alias = conn.execute(
                "SELECT alias FROM scryfall_tag_aliases WHERE tag_id='art-tag-id'"
            ).fetchone()[0]
            art_assignment = conn.execute(
                "SELECT illustration_id FROM art_tag_assignments WHERE tag_id='art-tag-id'"
            ).fetchone()[0]
            oracle_assignment = conn.execute(
                "SELECT oracle_id FROM oracle_tag_assignments WHERE tag_id='oracle-tag-id'"
            ).fetchone()[0]
            closure = conn.execute(
                "SELECT depth FROM scryfall_tag_closure WHERE ancestor_tag_id='parent-id' AND descendant_tag_id='art-tag-id'"
            ).fetchone()[0]
        self.assertEqual(art_alias, "sky island")
        self.assertEqual(art_assignment, "illustration-1")
        self.assertEqual(oracle_assignment, "oracle-1")
        self.assertEqual(closure, 1)

    def test_invalid_tag_snapshot_rolls_back_instead_of_erasing_existing_tags(self):
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO scryfall_tags(id,slug,label,tag_type) VALUES(?,?,?,?)",
                ("existing", "existing-tag", "Existing tag", "art"),
            )
        art = self.write_jsonl("bad-art.jsonl.gz", [{
            "id": "empty-art",
            "slug": "empty-art",
            "label": "Empty art tag",
            "taggings": [],
        }])
        oracle = self.write_jsonl("oracle.jsonl.gz", [{
            "id": "oracle-tag",
            "slug": "ramp",
            "label": "Ramp",
            "taggings": [{"oracle_id": "oracle-1"}],
        }])

        with self.assertRaisesRegex(RuntimeError, "no illustration taggings"):
            updater.import_tag_snapshots(art, oracle, {})
        with self.connect() as conn:
            ids = [row[0] for row in conn.execute("SELECT id FROM scryfall_tags")]
        self.assertEqual(ids, ["existing"])

    def test_missing_image_hashes_are_generated_and_saved(self):
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO card_art(scryfall_id,name,image_uri) VALUES(?,?,?)",
                ("pending-id", "Pending Card", "https://cards.scryfall.io/test.jpg"),
            )
        with mock.patch.object(
            updater, "hash_image",
            return_value=("https://cards.scryfall.io/test.jpg", "abcdef0123456789", None),
        ):
            stats = updater.fill_missing_hashes()
        self.assertEqual(stats, {"attempted": 1, "updated": 1, "failed": 0})
        with self.connect() as conn:
            saved = conn.execute(
                "SELECT phash FROM card_art WHERE scryfall_id='pending-id'"
            ).fetchone()[0]
        self.assertEqual(saved, "abcdef0123456789")


if __name__ == "__main__":
    unittest.main()
