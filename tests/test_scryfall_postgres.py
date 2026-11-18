"""Transactional integration tests for the PostgreSQL Scryfall importer."""

from __future__ import annotations

import gzip
import json
import os
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4
from unittest import mock

from postgres_db import close_pool, connection, open_pool, run_migrations

try:
    import scryfall_postgres as importer
except ModuleNotFoundError:
    importer = None


@unittest.skipUnless(
    os.environ.get("CARD_SCANNER_POSTGRES_DSN"),
    "CARD_SCANNER_POSTGRES_DSN is not configured",
)
@unittest.skipIf(importer is None, "scryfall_postgres module is not implemented yet")
class PostgreSQLScryfallTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        open_pool()
        run_migrations()

    @classmethod
    def tearDownClass(cls):
        close_pool()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.token = uuid4().hex

    def tearDown(self):
        self.temp.cleanup()

    def write_jsonl(self, name, records):
        path = self.root / name
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
        return path

    def rollback_fixture(self, callback):
        """Run a test fixture in a transaction that is always rolled back."""
        class RollbackFixture(Exception):
            pass

        with connection() as conn:
            try:
                with conn.transaction():
                    callback(conn)
                    raise RollbackFixture()
            except RollbackFixture:
                pass

    def make_printing(self, printing_id, image_uri, oracle_id="oracle-test"):
        return {
            "id": printing_id,
            "oracle_id": oracle_id,
            "set_id": "set-test",
            "set": "tst",
            "set_name": "Test Set",
            "set_type": "expansion",
            "released_at": "2026-01-01",
            "name": "Two Face Test",
            "collector_number": "7",
            "rarity": "rare",
            "lang": "en",
            "finishes": ["nonfoil", "foil"],
            "type_line": "Creature — Test",
            "oracle_text": "Test rules text.",
            "mana_cost": "{1}{U}",
            "mana_value": 2,
            "colors": ["U"],
            "color_identity": ["U"],
            "legalities": {"standard": "legal"},
            "card_faces": [
                {
                    "name": "Front Face",
                    "type_line": "Creature — Front",
                    "oracle_text": "Front rules.",
                    "flavor_text": "Front flavor.",
                    "oracle_id": oracle_id,
                    "illustration_id": "illustration-front",
                    "image_uris": {"normal": image_uri},
                },
                {
                    "name": "Back Face",
                    "type_line": "Creature — Back",
                    "oracle_text": "Back rules.",
                    "flavor_text": "Back flavor.",
                    "oracle_id": oracle_id,
                    "illustration_id": "illustration-back",
                    "image_uris": {
                        "normal": "https://fixture.invalid/back.jpg"
                    },
                },
            ],
        }

    def test_single_face_uses_top_level_image_when_face_image_missing(self):
        printing_id = f"single-face-fallback-{self.token}"
        oracle_id = f"single-face-oracle-{self.token}"
        image_uri = f"https://fixture.invalid/single-face-{self.token}.jpg"

        source_file = self.write_jsonl("single-face-fallback.jsonl.gz", [{
            "id": printing_id,
            "oracle_id": oracle_id,
            "set_id": f"single-face-set-{self.token}",
            "set": "tst",
            "set_name": "Single Face Test Set",
            "set_type": "expansion",
            "released_at": "2026-01-01",
            "name": "Single Face Fallback",
            "collector_number": "1",
            "image_uris": {"normal": image_uri},
            "card_faces": [{
                "name": "Single Face Fallback",
                "oracle_id": oracle_id,
                "illustration_id": f"single-face-illustration-{self.token}",
                "type_line": "Creature",
                "oracle_text": "A single face without its own image URI.",
            }],
        }])

        def exercise(conn):
            importer.import_printings(
                conn, source_file, "2026-11-14T15:00:00Z"
            )

            row = conn.execute("""
                SELECT
                    printing.image_id AS printing_image_id,
                    face.image_id AS face_image_id,
                    image.image_uri
                FROM catalog.external_ids AS external
                JOIN catalog.printings AS printing
                  ON printing.printing_id = external.printing_id
                 AND printing.game_id = external.game_id
                JOIN catalog.printing_faces AS face
                  ON face.printing_id = printing.printing_id
                 AND face.game_id = printing.game_id
                 AND face.face_index = 0
                LEFT JOIN catalog.images AS image
                  ON image.image_id = face.image_id
                 AND image.game_id = face.game_id
                WHERE external.game_id = 'mtg'
                  AND external.provider_id = 'scryfall'
                  AND external.entity_type = 'printing'
                  AND external.external_id_type = 'scryfall_id'
                  AND external.external_id = %s
            """, (printing_id,)).fetchone()

            self.assertIsNotNone(row)
            self.assertEqual(row["image_uri"], image_uri)
            self.assertIsNotNone(row["printing_image_id"])
            self.assertEqual(row["face_image_id"], row["printing_image_id"])

        self.rollback_fixture(exercise)


    def test_printing_import_is_idempotent_and_preserves_image_hashes(self):
        printing_id = f"printing-{self.token}"
        source = self.write_jsonl(
            "printings.jsonl.gz",
            [
                self.make_printing(
                    printing_id,
                    "https://fixture.invalid/front.jpg",
                    oracle_id=f"oracle-{self.token}",
                ),
                {
                    "id": f"no-oracle-{self.token}",
                    "name": "Unresolved Card",
                    "set": "tst",
                    "collector_number": "8",
                    "illustration_id": f"unresolved-illustration-{self.token}",
                    "image_uris": {
                        "normal": "https://fixture.invalid/unresolved.jpg"
                    },
                },
            ],
        )

        def exercise(conn):
            stats = importer.import_printings(
                conn, source, "2026-11-14T12:00:00Z"
            )
            self.assertEqual(stats["printings"], 2)

            printing = conn.execute(
                """
                SELECT p.printing_id, p.card_id, p.set_id, p.image_id
                FROM catalog.external_ids AS e
                JOIN catalog.printings AS p
                  ON p.printing_id = e.printing_id
                 AND p.game_id = e.game_id
                WHERE e.game_id = 'mtg'
                  AND e.provider_id = 'scryfall'
                  AND e.external_id_type = 'scryfall_id'
                  AND e.external_id = %s
                """,
                (printing_id,),
            ).fetchone()

            self.assertIsNotNone(printing)
            self.assertIsNotNone(printing["card_id"])
            self.assertIsNotNone(printing["set_id"])
            original_printing_id = printing["printing_id"]
            original_image_id = printing["image_id"]

            faces = conn.execute(
                """
                SELECT pf.face_index, pf.name, pf.illustration_id,
                       i.image_uri
                FROM catalog.printing_faces AS pf
                LEFT JOIN catalog.images AS i
                  ON i.image_id = pf.image_id AND i.game_id = pf.game_id
                WHERE pf.printing_id = %s
                ORDER BY pf.face_index
                """,
                (original_printing_id,),
            ).fetchall()

            self.assertEqual(len(faces), 2)
            self.assertEqual(faces[0]["name"], "Front Face")
            self.assertEqual(faces[1]["name"], "Back Face")
            self.assertEqual(
                [row["image_uri"] for row in faces],
                [
                    "https://fixture.invalid/front.jpg",
                    "https://fixture.invalid/back.jpg",
                ],
            )

            conn.execute(
                """
                INSERT INTO catalog.image_hashes
                    (image_id, game_id, hash_algorithm, hash_version, hash_value)
                VALUES (%s, 'mtg', 'phash', '1', '0123456789abcdef')
                """,
                (original_image_id,),
            )

            # Re-importing the exact source must retain internal identities
            # and the valid hash for the unchanged image resource.
            importer.import_printings(conn, source, "2026-11-14T12:00:00Z")

            repeated = conn.execute(
                """
                SELECT p.printing_id, p.image_id
                FROM catalog.external_ids AS e
                JOIN catalog.printings AS p
                  ON p.printing_id = e.printing_id AND p.game_id = e.game_id
                WHERE e.game_id = 'mtg'
                  AND e.provider_id = 'scryfall'
                  AND e.external_id_type = 'scryfall_id'
                  AND e.external_id = %s
                """,
                (printing_id,),
            ).fetchone()

            self.assertEqual(repeated["printing_id"], original_printing_id)
            self.assertEqual(repeated["image_id"], original_image_id)

            saved_hash = conn.execute(
                """
                SELECT hash_value
                FROM catalog.image_hashes
                WHERE image_id = %s AND hash_algorithm = 'phash'
                """,
                (original_image_id,),
            ).fetchone()
            self.assertEqual(saved_hash["hash_value"], "0123456789abcdef")

            unresolved = conn.execute(
                """
                SELECT p.card_id
                FROM catalog.external_ids AS e
                JOIN catalog.printings AS p
                  ON p.printing_id = e.printing_id AND p.game_id = e.game_id
                WHERE e.game_id = 'mtg'
                  AND e.provider_id = 'scryfall'
                  AND e.external_id_type = 'scryfall_id'
                  AND e.external_id = %s
                """,
                (f"no-oracle-{self.token}",),
            ).fetchone()
            self.assertIsNone(unresolved["card_id"])

        self.rollback_fixture(exercise)

    def test_changed_image_url_uses_a_new_unhashed_image_resource(self):
        printing_id = f"changed-image-{self.token}"
        original_url = "https://fixture.invalid/original.jpg"
        changed_url = "https://fixture.invalid/changed.jpg"
        source = self.write_jsonl(
            "changing.jsonl.gz",
            [
                {
                    "id": printing_id,
                    "name": "Changing Image",
                    "set": "tst",
                    "collector_number": "9",
                    "image_uris": {"normal": original_url},
                }
            ],
        )

        def exercise(conn):
            importer.import_printings(conn, source, "2026-11-14T12:00:00Z")

            first = conn.execute(
                """
                SELECT p.image_id
                FROM catalog.external_ids AS e
                JOIN catalog.printings AS p
                  ON p.printing_id = e.printing_id AND p.game_id = e.game_id
                WHERE e.external_id_type = 'scryfall_id'
                  AND e.external_id = %s
                """,
                (printing_id,),
            ).fetchone()
            old_image_id = first["image_id"]

            conn.execute(
                """
                INSERT INTO catalog.image_hashes
                    (image_id, game_id, hash_algorithm, hash_version, hash_value)
                VALUES (%s, 'mtg', 'phash', '1', 'fedcba9876543210')
                """,
                (old_image_id,),
            )

            changed_record = {
                "id": printing_id,
                "name": "Changing Image",
                "set": "tst",
                "collector_number": "9",
                "image_uris": {"normal": changed_url},
            }
            changed_source = self.write_jsonl(
                "changed.jsonl.gz", [changed_record]
            )
            importer.import_printings(
                conn, changed_source, "2026-11-14T13:00:00Z"
            )

            current = conn.execute(
                """
                SELECT p.image_id, i.image_uri
                FROM catalog.external_ids AS e
                JOIN catalog.printings AS p
                  ON p.printing_id = e.printing_id AND p.game_id = e.game_id
                JOIN catalog.images AS i
                  ON i.image_id = p.image_id AND i.game_id = p.game_id
                WHERE e.external_id_type = 'scryfall_id'
                  AND e.external_id = %s
                """,
                (printing_id,),
            ).fetchone()

            self.assertNotEqual(current["image_id"], old_image_id)
            self.assertEqual(current["image_uri"], changed_url)

            new_hash = conn.execute(
                """
                SELECT hash_value FROM catalog.image_hashes
                WHERE image_id = %s AND hash_algorithm = 'phash'
                """,
                (current["image_id"],),
            ).fetchone()
            self.assertIsNone(new_hash)

            old_hash = conn.execute(
                """
                SELECT hash_value FROM catalog.image_hashes
                WHERE image_id = %s AND hash_algorithm = 'phash'
                """,
                (old_image_id,),
            ).fetchone()
            self.assertEqual(old_hash["hash_value"], "fedcba9876543210")

        self.rollback_fixture(exercise)

    def test_tag_import_preserves_unresolved_art_and_oracle_ids(self):
        art = self.write_jsonl(
            "art-tags.jsonl.gz",
            [
                {
                    "id": f"art-parent-{self.token}",
                    "slug": "parent",
                    "label": "Parent",
                    "aliases": ["root alias", "ROOT ALIAS"],
                    "taggings": [
                        {
                            "illustration_id": f"orphan-art-{self.token}",
                            "weight": "high",
                            "annotation": "unresolved artwork",
                        }
                    ],
                },
                {
                    "id": f"art-child-{self.token}",
                    "slug": "child",
                    "label": "Child",
                    "parent_ids": [f"art-parent-{self.token}"],
                    "aliases": ["shared alias"],
                    "taggings": [
                        {
                            "illustration_id": f"orphan-art-{self.token}",
                            "weight": "median",
                        }
                    ],
                },
            ],
        )
        oracle = self.write_jsonl(
            "oracle-tags.jsonl.gz",
            [
                {
                    "id": f"oracle-tag-{self.token}",
                    "slug": "ramp",
                    "label": "Ramp",
                    "taggings": [
                        {
                            "oracle_id": f"unknown-oracle-{self.token}",
                            "weight": "median",
                            "annotation": "unresolved oracle",
                        }
                    ],
                }
            ],
        )

        def exercise(conn):
            stats = importer.import_tag_snapshots(
                conn,
                art,
                oracle,
                {"art_tags": "art-version", "oracle_tags": "oracle-version"},
            )

            self.assertEqual(stats["tags"], 3)
            self.assertEqual(stats["aliases"], 2)
            self.assertEqual(stats["art_assignments"], 2)
            self.assertEqual(stats["oracle_assignments"], 1)

            illustration = conn.execute(
                """
                SELECT i.illustration_id
                FROM catalog.external_ids AS e
                JOIN catalog.illustrations AS i
                  ON i.illustration_id = e.illustration_id
                 AND i.game_id = e.game_id
                WHERE e.game_id = 'mtg'
                  AND e.provider_id = 'scryfall'
                  AND e.entity_type = 'illustration'
                  AND e.external_id_type = 'illustration_id'
                  AND e.external_id = %s
                """,
                (f"orphan-art-{self.token}",),
            ).fetchone()
            self.assertIsNotNone(illustration)

            assignment = conn.execute(
                """
                SELECT weight, annotation
                FROM catalog.art_tag_assignments
                WHERE illustration_id = %s
                ORDER BY weight
                """,
                (illustration["illustration_id"],),
            ).fetchall()
            self.assertEqual(len(assignment), 2)
            self.assertIn(
                ("high", "unresolved artwork"),
                [(row["weight"], row["annotation"]) for row in assignment],
            )

            closure = conn.execute(
                """
                SELECT tc.depth
                FROM catalog.tag_closure AS tc
                JOIN catalog.tags AS ancestor
                  ON ancestor.tag_id = tc.ancestor_tag_id
                JOIN catalog.tags AS descendant
                  ON descendant.tag_id = tc.descendant_tag_id
                WHERE ancestor.external_tag_id = %s
                  AND descendant.external_tag_id = %s
                  AND tc.game_id = 'mtg'
                  AND tc.provider_id = 'scryfall'
                """,
                (
                    f"art-parent-{self.token}",
                    f"art-child-{self.token}",
                ),
            ).fetchone()
            self.assertEqual(closure["depth"], 1)

            oracle_assignment = conn.execute(
                """
                SELECT external_oracle_id, card_id, weight, annotation
                FROM catalog.oracle_tag_assignments AS a
                JOIN catalog.tags AS t USING (tag_id, game_id, provider_id)
                WHERE t.external_tag_id = %s
                """,
                (f"oracle-tag-{self.token}",),
            ).fetchone()
            self.assertEqual(
                oracle_assignment["external_oracle_id"],
                f"unknown-oracle-{self.token}",
            )
            self.assertIsNone(oracle_assignment["card_id"])
            self.assertEqual(oracle_assignment["annotation"], "unresolved oracle")

        self.rollback_fixture(exercise)

    def test_invalid_tag_snapshot_does_not_replace_existing_snapshot(self):
        valid_art = self.write_jsonl(
            "valid-art.jsonl.gz",
            [
                {
                    "id": f"valid-art-{self.token}",
                    "slug": "valid",
                    "label": "Valid",
                    "taggings": [
                        {"illustration_id": f"illustration-{self.token}"}
                    ],
                }
            ],
        )
        valid_oracle = self.write_jsonl(
            "valid-oracle.jsonl.gz",
            [
                {
                    "id": f"valid-oracle-{self.token}",
                    "slug": "valid-oracle",
                    "label": "Valid Oracle",
                    "taggings": [{"oracle_id": f"oracle-{self.token}"}],
                }
            ],
        )
        invalid_art = self.write_jsonl(
            "invalid-art.jsonl.gz",
            [
                {
                    "id": f"invalid-art-{self.token}",
                    "slug": "invalid",
                    "label": "Invalid",
                    "parent_ids": [f"missing-parent-{self.token}"],
                    "taggings": [
                        {"illustration_id": f"illustration-{self.token}"}
                    ],
                }
            ],
        )

        def exercise(conn):
            importer.import_tag_snapshots(
                conn, valid_art, valid_oracle, {}
            )

            with self.assertRaises(RuntimeError):
                with conn.transaction():
                    importer.import_tag_snapshots(
                        conn, invalid_art, valid_oracle, {}
                    )

            existing = conn.execute(
                """
                SELECT slug
                FROM catalog.tags
                WHERE game_id = 'mtg'
                  AND provider_id = 'scryfall'
                  AND external_tag_id = %s
                """,
                (f"valid-art-{self.token}",),
            ).fetchone()
            self.assertEqual(existing["slug"], "valid")

            invalid = conn.execute(
                """
                SELECT 1 FROM catalog.tags
                WHERE game_id = 'mtg'
                  AND provider_id = 'scryfall'
                  AND external_tag_id = %s
                """,
                (f"invalid-art-{self.token}",),
            ).fetchone()
            self.assertIsNone(invalid)

        self.rollback_fixture(exercise)


    def test_cyclic_tag_hierarchy_is_rejected_without_replacing_snapshot(self):
        valid_art = self.write_jsonl("cycle-valid-art.jsonl.gz", [{
            "id": f"valid-art-{self.token}",
            "slug": "valid",
            "label": "Valid",
            "taggings": [{
                "illustration_id": f"cycle-illustration-{self.token}"
            }],
        }])
        valid_oracle = self.write_jsonl("cycle-valid-oracle.jsonl.gz", [{
            "id": f"valid-oracle-{self.token}",
            "slug": "valid-oracle",
            "label": "Valid Oracle",
            "taggings": [{"oracle_id": f"cycle-oracle-{self.token}"}],
        }])
        cyclic_art = self.write_jsonl("cycle-invalid-art.jsonl.gz", [
            {
                "id": f"cycle-a-{self.token}",
                "slug": "cycle-a",
                "label": "Cycle A",
                "parent_ids": [f"cycle-b-{self.token}"],
                "taggings": [{
                    "illustration_id": f"cycle-illustration-{self.token}"
                }],
            },
            {
                "id": f"cycle-b-{self.token}",
                "slug": "cycle-b",
                "label": "Cycle B",
                "parent_ids": [f"cycle-a-{self.token}"],
                "taggings": [{
                    "illustration_id": f"cycle-illustration-{self.token}"
                }],
            },
        ])

        def exercise(conn):
            importer.import_tag_snapshots(
                conn, valid_art, valid_oracle, {}
            )

            with self.assertRaisesRegex(RuntimeError, "cycle"):
                importer.import_tag_snapshots(
                    conn, cyclic_art, valid_oracle, {}
                )

            existing = conn.execute("""
                SELECT slug
                FROM catalog.tags
                WHERE game_id = 'mtg'
                  AND provider_id = 'scryfall'
                  AND external_tag_id = %s
            """, (f"valid-art-{self.token}",)).fetchone()
            self.assertEqual(existing["slug"], "valid")

            cycle_rows = conn.execute("""
                SELECT COUNT(*) AS count
                FROM catalog.tags
                WHERE game_id = 'mtg'
                  AND provider_id = 'scryfall'
                  AND external_tag_id IN (%s, %s)
            """, (f"cycle-a-{self.token}", f"cycle-b-{self.token}")).fetchone()
            self.assertEqual(cycle_rows["count"], 0)

        self.rollback_fixture(exercise)

    def test_duplicate_source_tag_ids_are_rejected(self):
        duplicate_id = f"duplicate-{self.token}"
        duplicate_art = self.write_jsonl("duplicate-art.jsonl.gz", [
            {
                "id": duplicate_id,
                "slug": "duplicate-one",
                "label": "Duplicate One",
                "taggings": [{
                    "illustration_id": f"duplicate-illustration-{self.token}"
                }],
            },
            {
                "id": duplicate_id,
                "slug": "duplicate-two",
                "label": "Duplicate Two",
                "taggings": [{
                    "illustration_id": f"duplicate-illustration-{self.token}"
                }],
            },
        ])
        oracle = self.write_jsonl("duplicate-oracle.jsonl.gz", [{
            "id": f"duplicate-oracle-tag-{self.token}",
            "slug": "oracle",
            "label": "Oracle",
            "taggings": [{"oracle_id": f"duplicate-oracle-{self.token}"}],
        }])

        def exercise(conn):
            with self.assertRaisesRegex(RuntimeError, "duplicate IDs"):
                importer.import_tag_snapshots(
                    conn, duplicate_art, oracle, {}
                )

            rows = conn.execute("""
                SELECT COUNT(*) AS count
                FROM catalog.tags
                WHERE game_id = 'mtg'
                  AND provider_id = 'scryfall'
                  AND external_tag_id = %s
            """, (duplicate_id,)).fetchone()
            self.assertEqual(rows["count"], 0)

        self.rollback_fixture(exercise)

    def test_repeated_tag_snapshot_import_preserves_ids_without_duplicates(self):
        parent_id = f"repeat-parent-{self.token}"
        child_id = f"repeat-child-{self.token}"
        illustration_id = f"repeat-illustration-{self.token}"
        oracle_tag_id = f"repeat-oracle-tag-{self.token}"

        art = self.write_jsonl("repeat-art.jsonl.gz", [
            {
                "id": parent_id,
                "slug": "repeat-parent",
                "label": "Repeat Parent",
                "aliases": ["parent alias", "PARENT ALIAS"],
                "taggings": [{
                    "illustration_id": illustration_id,
                    "weight": "high",
                }],
            },
            {
                "id": child_id,
                "slug": "repeat-child",
                "label": "Repeat Child",
                "parent_ids": [parent_id],
                "taggings": [{
                    "illustration_id": illustration_id,
                    "weight": "median",
                }],
            },
        ])
        oracle = self.write_jsonl("repeat-oracle.jsonl.gz", [{
            "id": oracle_tag_id,
            "slug": "repeat-oracle",
            "label": "Repeat Oracle",
            "taggings": [{
                "oracle_id": f"repeat-oracle-{self.token}"
            }],
        }])

        expected = {
            "tags": 3,
            "aliases": 1,
            "art_assignments": 2,
            "oracle_assignments": 1,
        }

        def exercise(conn):
            first_stats = importer.import_tag_snapshots(
                conn, art, oracle,
                {"art_tags": "repeat-a", "oracle_tags": "repeat-o"},
            )
            first_tag = conn.execute("""
                SELECT tag_id
                FROM catalog.tags
                WHERE game_id = 'mtg'
                  AND provider_id = 'scryfall'
                  AND external_tag_id = %s
            """, (parent_id,)).fetchone()["tag_id"]

            second_stats = importer.import_tag_snapshots(
                conn, art, oracle,
                {"art_tags": "repeat-a", "oracle_tags": "repeat-o"},
            )
            second_tag = conn.execute("""
                SELECT tag_id
                FROM catalog.tags
                WHERE game_id = 'mtg'
                  AND provider_id = 'scryfall'
                  AND external_tag_id = %s
            """, (parent_id,)).fetchone()["tag_id"]

            self.assertEqual(first_stats, expected)
            self.assertEqual(second_stats, expected)
            self.assertEqual(first_tag, second_tag)

            counts = conn.execute("""
                SELECT
                    (SELECT COUNT(*) FROM catalog.tags
                     WHERE game_id = 'mtg'
                       AND provider_id = 'scryfall'
                       AND external_tag_id IN (%s, %s, %s)) AS tags,
                    (SELECT COUNT(*) FROM catalog.tag_aliases AS a
                     JOIN catalog.tags AS t USING (tag_id, game_id, provider_id)
                     WHERE t.game_id = 'mtg'
                       AND t.provider_id = 'scryfall'
                       AND t.external_tag_id = %s) AS aliases,
                    (SELECT COUNT(*) FROM catalog.art_tag_assignments AS a
                     JOIN catalog.tags AS t USING (tag_id, game_id, provider_id)
                     WHERE t.game_id = 'mtg'
                       AND t.provider_id = 'scryfall'
                       AND t.external_tag_id IN (%s, %s)) AS art_assignments,
                    (SELECT COUNT(*) FROM catalog.oracle_tag_assignments AS a
                     JOIN catalog.tags AS t USING (tag_id, game_id, provider_id)
                     WHERE t.game_id = 'mtg'
                       AND t.provider_id = 'scryfall'
                       AND t.external_tag_id = %s) AS oracle_assignments
            """, (
                parent_id, child_id, oracle_tag_id,
                parent_id,
                parent_id, child_id,
                oracle_tag_id,
            )).fetchone()

            self.assertEqual(
                tuple(counts.values()),
                (3, 1, 2, 1),
            )

        self.rollback_fixture(exercise)


    def test_hash_worker_generates_phash_and_skips_hashed_images(self):
        image_uri = f"https://fixture.invalid/hash-{self.token}.jpg"

        def exercise(conn):
            image_id = conn.execute("""
                INSERT INTO catalog.images (
                    game_id, provider_id, image_uri, image_role
                )
                VALUES ('mtg', 'scryfall', %s, 'card_face')
                RETURNING image_id
            """, (image_uri,)).fetchone()["image_id"]

            with mock.patch.object(
                importer,
                "hash_image",
                return_value=(image_uri, "0123456789abcdef", None),
            ) as hash_mock:
                stats = importer.fill_missing_hashes(
                    conn, workers=1, image_ids=[image_id]
                )

            self.assertEqual(
                stats, {"attempted": 1, "updated": 1, "failed": 0}
            )
            hash_mock.assert_called_once_with(image_uri)

            saved = conn.execute("""
                SELECT hash_algorithm, hash_version, hash_value
                FROM catalog.image_hashes
                WHERE image_id = %s
            """, (image_id,)).fetchone()
            self.assertEqual(
                tuple(saved.values()),
                ("phash", "1", "0123456789abcdef"),
            )

            with mock.patch.object(importer, "hash_image") as second_mock:
                second_stats = importer.fill_missing_hashes(
                    conn, workers=1, image_ids=[image_id]
                )

            self.assertEqual(
                second_stats, {"attempted": 0, "updated": 0, "failed": 0}
            )
            second_mock.assert_not_called()

        self.rollback_fixture(exercise)

    def test_stale_hash_result_is_rejected_after_image_uri_changes(self):
        original_uri = f"https://fixture.invalid/old-{self.token}.jpg"
        replacement_uri = f"https://fixture.invalid/new-{self.token}.jpg"

        def exercise(conn):
            image_id = conn.execute("""
                INSERT INTO catalog.images (
                    game_id, provider_id, image_uri, image_role
                )
                VALUES ('mtg', 'scryfall', %s, 'card_face')
                RETURNING image_id
            """, (original_uri,)).fetchone()["image_id"]

            # Simulate the resource changing while an older download is in flight.
            conn.execute("""
                UPDATE catalog.images
                SET image_uri = %s, updated_at = now()
                WHERE image_id = %s AND game_id = 'mtg'
            """, (replacement_uri, image_id))

            stored = importer._store_image_hash(
                conn,
                image_id,
                original_uri,
                "fedcba9876543210",
            )
            self.assertFalse(stored)

            absent = conn.execute("""
                SELECT 1
                FROM catalog.image_hashes
                WHERE image_id = %s
                  AND hash_algorithm = 'phash'
                  AND hash_version = '1'
            """, (image_id,)).fetchone()
            self.assertIsNone(absent)

            # The current resource can still receive its own correct hash.
            stored_current = importer._store_image_hash(
                conn,
                image_id,
                replacement_uri,
                "0011223344556677",
            )
            self.assertTrue(stored_current)

            current = conn.execute("""
                SELECT hash_value
                FROM catalog.image_hashes
                WHERE image_id = %s AND hash_algorithm = 'phash'
            """, (image_id,)).fetchone()
            self.assertEqual(current["hash_value"], "0011223344556677")

        self.rollback_fixture(exercise)


    def test_hash_worker_paginates_and_respects_limit(self):
        image_uris = [
            f"https://fixture.invalid/batch-{index}-{self.token}.jpg"
            for index in range(3)
        ]

        def exercise(conn):
            image_ids = []
            for uri in image_uris:
                image_row = conn.execute("""
                    INSERT INTO catalog.images (
                        game_id, provider_id, image_uri, image_role
                    )
                    VALUES ('mtg', 'scryfall', %s, 'card_face')
                    RETURNING image_id
                """, (uri,)).fetchone()
                image_ids.append(int(image_row["image_id"]))

            def fake_hash(uri):
                return uri, f"hash-{image_uris.index(uri):016x}", None

            with mock.patch.object(
                importer, "hash_image", side_effect=fake_hash
            ) as hash_mock:
                first = importer.fill_missing_hashes(
                    conn, limit=2, workers=2, batch_size=1,
                    image_ids=image_ids,
                )
                self.assertEqual(
                    first, {"attempted": 2, "updated": 2, "failed": 0}
                )
                self.assertEqual(hash_mock.call_count, 2)

                # A new invocation must find the remaining image, even
                # though the previous invocation stopped at its limit.
                second = importer.fill_missing_hashes(
                    conn, workers=2, batch_size=1,
                    image_ids=image_ids,
                )
                self.assertEqual(
                    second, {"attempted": 1, "updated": 1, "failed": 0}
                )
                self.assertEqual(hash_mock.call_count, 3)

            saved = conn.execute("""
                SELECT COUNT(*) AS count
                FROM catalog.image_hashes AS h
                JOIN catalog.images AS i
                  ON i.image_id = h.image_id AND i.game_id = h.game_id
                WHERE i.game_id = 'mtg'
                  AND i.provider_id = 'scryfall'
                  AND i.image_uri = ANY(%s)
                  AND h.hash_algorithm = 'phash'
                  AND h.hash_version = '1'
            """, (image_uris,)).fetchone()
            self.assertEqual(saved["count"], 3)

        self.rollback_fixture(exercise)


    def test_printing_import_rejects_duplicate_ids_across_batches(self):
        duplicate_id = f"duplicate-printing-{self.token}"
        first_image = f"https://fixture.invalid/duplicate-first-{self.token}.jpg"
        second_image = f"https://fixture.invalid/duplicate-second-{self.token}.jpg"
        source_file = self.write_jsonl("duplicate-printing.jsonl.gz", [
            {
                "id": duplicate_id,
                "name": "First duplicate occurrence",
                "collector_number": "1",
                "released_at": "2026-01-01",
                "image_uris": {"normal": first_image},
            },
            {
                "id": duplicate_id,
                "name": "Second duplicate occurrence",
                "collector_number": "2",
                "released_at": "2026-01-01",
                "image_uris": {"normal": second_image},
            },
        ])

        def exercise(conn):
            with self.assertRaisesRegex(RuntimeError, "duplicate IDs"):
                importer.import_printings(
                    conn,
                    source_file,
                    "2026-11-14T15:00:00Z",
                    batch_size=1,
                )

            printing_count = conn.execute("""
                SELECT COUNT(*) AS count
                FROM catalog.external_ids
                WHERE game_id = 'mtg'
                  AND provider_id = 'scryfall'
                  AND entity_type = 'printing'
                  AND external_id_type = 'scryfall_id'
                  AND external_id = %s
            """, (duplicate_id,)).fetchone()["count"]
            self.assertEqual(printing_count, 0)

            image_count = conn.execute("""
                SELECT COUNT(*) AS count
                FROM catalog.images
                WHERE game_id = 'mtg'
                  AND provider_id = 'scryfall'
                  AND image_uri IN (%s, %s)
            """, (first_image, second_image)).fetchone()["count"]
            self.assertEqual(image_count, 0)

        self.rollback_fixture(exercise)


    def test_printing_import_handles_multiple_batches_and_shared_entities(self):
        oracle_id = f"shared-oracle-{self.token}"
        set_id = f"shared-set-{self.token}"
        image_uri = f"https://fixture.invalid/shared-{self.token}.jpg"

        records = [
            {
                "id": f"batch-printing-{index}-{self.token}",
                "oracle_id": oracle_id,
                "set_id": set_id,
                "set": "tst",
                "set_name": "Shared Test Set",
                "set_type": "expansion",
                "released_at": "2026-01-01",
                "name": "Shared Test Card",
                "collector_number": str(index),
                "rarity": "rare",
                "lang": "en",
                "finishes": ["nonfoil"],
                "image_uris": {"normal": image_uri},
            }
            for index in range(5)
        ]
        source_file = self.write_jsonl("multi-batch.jsonl.gz", records)

        def exercise(conn):
            stats = importer.import_printings(
                conn,
                source_file,
                "2026-11-14T15:00:00Z",
                batch_size=2,
            )
            self.assertEqual(stats, {"printings": 5, "with_images": 5})

            printing_count = conn.execute("""
                SELECT COUNT(*) AS count
                FROM catalog.external_ids
                WHERE game_id = 'mtg'
                  AND provider_id = 'scryfall'
                  AND entity_type = 'printing'
                  AND external_id_type = 'scryfall_id'
                  AND external_id LIKE %s
            """, (f"batch-printing-%-{self.token}",)).fetchone()["count"]
            self.assertEqual(printing_count, 5)

            card_count = conn.execute("""
                SELECT COUNT(*) AS count
                FROM catalog.external_ids
                WHERE game_id = 'mtg'
                  AND provider_id = 'scryfall'
                  AND entity_type = 'card'
                  AND external_id_type = 'oracle_id'
                  AND external_id = %s
            """, (oracle_id,)).fetchone()["count"]
            self.assertEqual(card_count, 1)

            set_count = conn.execute("""
                SELECT COUNT(*) AS count
                FROM catalog.external_ids
                WHERE game_id = 'mtg'
                  AND provider_id = 'scryfall'
                  AND entity_type = 'set'
                  AND external_id_type = 'set_id'
                  AND external_id = %s
            """, (set_id,)).fetchone()["count"]
            self.assertEqual(set_count, 1)

            image_count = conn.execute("""
                SELECT COUNT(*) AS count
                FROM catalog.images
                WHERE game_id = 'mtg'
                  AND provider_id = 'scryfall'
                  AND image_uri = %s
            """, (image_uri,)).fetchone()["count"]
            self.assertEqual(image_count, 1)

        self.rollback_fixture(exercise)


    def test_printing_import_rolls_back_all_batches_after_late_failure(self):
        first_id = f"rollback-first-{self.token}"
        second_id = f"rollback-second-{self.token}"

        source_file = self.write_jsonl("late-failure.jsonl.gz", [
            {
                "id": first_id,
                "name": "Valid First Batch",
                "set": "tst",
                "collector_number": "1",
                "image_uris": {
                    "normal": "https://fixture.invalid/rollback-first.jpg"
                },
            },
            {
                "id": second_id,
                "name": "Invalid Second Batch",
                "set": "tst",
                "collector_number": "2",
                "released_at": "not-a-valid-date",
            },
        ])

        def exercise(conn):
            with self.assertRaisesRegex(RuntimeError, "Invalid Scryfall date"):
                importer.import_printings(
                    conn,
                    source_file,
                    "2026-11-14T15:00:00Z",
                    batch_size=1,
                )

            rows = conn.execute("""
                SELECT COUNT(*) AS count
                FROM catalog.external_ids
                WHERE game_id = 'mtg'
                  AND provider_id = 'scryfall'
                  AND entity_type = 'printing'
                  AND external_id_type = 'scryfall_id'
                  AND external_id IN (%s, %s)
            """, (first_id, second_id)).fetchone()
            self.assertEqual(rows["count"], 0)

            image_rows = conn.execute("""
                SELECT COUNT(*) AS count
                FROM catalog.images
                WHERE game_id = 'mtg'
                  AND provider_id = 'scryfall'
                  AND image_uri LIKE '%rollback-first.jpg'
            """).fetchone()
            self.assertEqual(image_rows["count"], 0)

        self.rollback_fixture(exercise)


if __name__ == "__main__":
    unittest.main()
