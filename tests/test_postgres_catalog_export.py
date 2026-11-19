"""Integration tests for the PostgreSQL-to-legacy-SQLite catalog export."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from uuid import uuid4

from postgres_db import close_pool, connection, open_pool, run_migrations
from postgres_catalog_export import export_mtg_legacy_art_catalog

try:
    import scryfall_postgres as importer
except ModuleNotFoundError:
    importer = None


@unittest.skipUnless(
    os.environ.get("CARD_SCANNER_POSTGRES_DSN"),
    "CARD_SCANNER_POSTGRES_DSN is not configured",
)
@unittest.skipIf(importer is None, "PostgreSQL Scryfall importer is unavailable")
class PostgreSQLCatalogExportTests(unittest.TestCase):
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
        import gzip

        path = self.root / name
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
        return path

    def rollback_fixture(self, callback):
        class RollbackFixture(Exception):
            pass

        with connection() as conn:
            try:
                with conn.transaction():
                    callback(conn)
                    raise RollbackFixture()
            except RollbackFixture:
                pass

    def test_export_preserves_scanner_schema_and_catalog_relationships(self):
        printing_id = f"export-printing-{self.token}"
        oracle_id = f"export-oracle-{self.token}"
        front_illustration = f"export-front-illustration-{self.token}"
        back_illustration = f"export-back-illustration-{self.token}"
        front_uri = f"https://fixture.invalid/export-front-{self.token}.jpg"
        back_uri = f"https://fixture.invalid/export-back-{self.token}.jpg"
        parent_tag_id = f"export-parent-tag-{self.token}"
        art_tag_id = f"export-art-tag-{self.token}"
        oracle_tag_id = f"export-oracle-tag-{self.token}"
        art_alias = f"export-art-alias-{self.token}"
        oracle_assignment_id = f"export-unknown-oracle-{self.token}"
        expected_hash = "0123456789abcdef"

        printing = {
            "id": printing_id,
            "oracle_id": oracle_id,
            "set_id": f"export-set-{self.token}",
            "set": "xts",
            "set_name": "Exporter Test Set",
            "set_type": "expansion",
            "released_at": "2026-01-01",
            "name": "Exporter Test Card",
            "collector_number": "42",
            "rarity": "rare",
            "lang": "en",
            "finishes": ["nonfoil", "foil"],
            "type_line": "Creature — Exporter",
            "oracle_text": "Exporter test rules.",
            "mana_cost": "{1}{U}",
            "mana_value": 2,
            "colors": ["U"],
            "color_identity": ["U"],
            "legalities": {"standard": "legal"},
            "card_faces": [
                {
                    "name": "Exporter Front",
                    "type_line": "Creature — Front",
                    "oracle_text": "Front rules.",
                    "flavor_text": "Front flavor.",
                    "oracle_id": oracle_id,
                    "illustration_id": front_illustration,
                    "image_uris": {"normal": front_uri},
                },
                {
                    "name": "Exporter Back",
                    "type_line": "Creature — Back",
                    "oracle_text": "Back rules.",
                    "flavor_text": "Back flavor.",
                    "oracle_id": oracle_id,
                    "illustration_id": back_illustration,
                    "image_uris": {"normal": back_uri},
                },
            ],
        }
        printings_path = self.write_jsonl("printings.jsonl.gz", [printing])
        art_tags_path = self.write_jsonl("art-tags.jsonl.gz", [
            {
                "id": parent_tag_id,
                "slug": "export-parent",
                "label": "Export Parent",
            },
            {
                "id": art_tag_id,
                "slug": "export-art",
                "label": "Export Art",
                "parent_ids": [parent_tag_id],
                "aliases": [art_alias],
                "taggings": [{
                    "illustration_id": front_illustration,
                    "weight": "high",
                    "annotation": "artwork fixture",
                }],
            },
        ])
        oracle_tags_path = self.write_jsonl("oracle-tags.jsonl.gz", [{
            "id": oracle_tag_id,
            "slug": "export-oracle",
            "label": "Export Oracle",
            "taggings": [{
                "oracle_id": oracle_assignment_id,
                "weight": "median",
                "annotation": "oracle fixture",
            }],
        }])

        output_path = self.root / "mtg_art_hashes.db"

        def exercise(conn):
            importer.import_printings(
                conn, printings_path, "2026-11-19T12:00:00Z"
            )
            importer.import_tag_snapshots(
                conn,
                art_tags_path,
                oracle_tags_path,
                {"art_tags": "art-test", "oracle_tags": "oracle-test"},
            )

            printing_row = conn.execute("""
                SELECT printing.image_id
                FROM catalog.external_ids AS external
                JOIN catalog.printings AS printing
                  ON printing.printing_id = external.printing_id
                 AND printing.game_id = external.game_id
                WHERE external.game_id = 'mtg'
                  AND external.provider_id = 'scryfall'
                  AND external.entity_type = 'printing'
                  AND external.external_id_type = 'scryfall_id'
                  AND external.external_id = %s
            """, (printing_id,)).fetchone()
            self.assertIsNotNone(printing_row)
            self.assertIsNotNone(printing_row["image_id"])
            importer._store_image_hash(
                conn,
                int(printing_row["image_id"]),
                front_uri,
                expected_hash,
            )

            info = export_mtg_legacy_art_catalog(conn, output_path)
            # CI seeds shared catalog fixtures before running this test, so
            # assert the isolated test rows rather than global table totals.
            self.assertGreaterEqual(info["card_art"], 1)
            self.assertGreaterEqual(info["card_art_faces"], 2)
            self.assertGreaterEqual(info["scryfall_tags"], 3)
            self.assertGreaterEqual(info["scryfall_tag_aliases"], 1)
            self.assertGreaterEqual(info["scryfall_tag_closure"], 1)
            self.assertGreaterEqual(info["art_tag_assignments"], 1)
            self.assertGreaterEqual(info["oracle_tag_assignments"], 1)
            self.assertGreaterEqual(info["phash"], 1)
            self.assertTrue(output_path.is_file())

            db = sqlite3.connect(str(output_path))
            try:
                self.assertEqual(
                    db.execute("PRAGMA integrity_check").fetchone()[0], "ok"
                )
                card = db.execute("""
                    SELECT name, set_code, set_name, rarity, collector_number,
                           oracle_id, phash, illustration_id, image_uri
                    FROM card_art WHERE scryfall_id = ?
                """, (printing_id,)).fetchone()
                self.assertIsNotNone(card)
                self.assertEqual(card, (
                    "Exporter Test Card",
                    "xts",
                    "Exporter Test Set",
                    "rare",
                    "42",
                    oracle_id,
                    expected_hash,
                    front_illustration,
                    front_uri,
                ))

                faces = db.execute("""
                    SELECT face_index, oracle_id, illustration_id, image_uri
                    FROM card_art_faces
                    WHERE scryfall_id = ?
                    ORDER BY face_index
                """, (printing_id,)).fetchall()
                self.assertEqual(faces, [
                    (0, oracle_id, front_illustration, front_uri),
                    (1, oracle_id, back_illustration, back_uri),
                ])

                tag = db.execute("""
                    SELECT slug, label, tag_type
                    FROM scryfall_tags WHERE id = ?
                """, (art_tag_id,)).fetchone()
                self.assertEqual(tag, ("export-art", "Export Art", "art"))
                self.assertEqual(
                    db.execute(
                        """
                        SELECT ancestor_tag_id, descendant_tag_id, depth
                        FROM scryfall_tag_closure
                        WHERE ancestor_tag_id = ? AND descendant_tag_id = ?
                        """,
                        (parent_tag_id, art_tag_id),
                    ).fetchone(),
                    (parent_tag_id, art_tag_id, 1),
                )
                self.assertEqual(
                    db.execute(
                        "SELECT tag_id, alias FROM scryfall_tag_aliases "
                        "WHERE tag_id = ?",
                        (art_tag_id,),
                    ).fetchone(),
                    (art_tag_id, art_alias),
                )
                self.assertEqual(
                    db.execute(
                        "SELECT tag_id, illustration_id, weight, annotation "
                        "FROM art_tag_assignments WHERE tag_id = ?",
                        (art_tag_id,),
                    ).fetchone(),
                    (art_tag_id, front_illustration, "high", "artwork fixture"),
                )
                self.assertEqual(
                    db.execute(
                        "SELECT tag_id, oracle_id, weight, annotation "
                        "FROM oracle_tag_assignments WHERE tag_id = ?",
                        (oracle_tag_id,),
                    ).fetchone(),
                    (
                        oracle_tag_id,
                        oracle_assignment_id,
                        "median",
                        "oracle fixture",
                    ),
                )
            finally:
                db.close()

            # Verify the existing desktop-client manifest/download contract
            # chooses the PostgreSQL-generated artifact when PG mode is active.
            import app as app_module

            snapshot_info = {
                "row_count": info["card_art"],
                "phash_count": info["phash"],
                "source_modified_at": str(info["source_updated_at"]),
                "catalog_revision": info["catalog_revision"],
            }
            api_key = "postgres-export-test-api-key"
            with (
                mock.patch.object(app_module, "DATABASE_BACKEND", "postgres"),
                mock.patch.object(app_module, "SCANNER_API_KEY", api_key),
                mock.patch.object(
                    app_module,
                    "_scanner_client_postgres_art_snapshot",
                    return_value=(output_path, snapshot_info),
                ),
            ):
                client = app_module.app.test_client()
                headers = {"X-Scanner-API-Key": api_key}
                manifest_response = client.get(
                    "/api/scanner/client-data/manifest",
                    headers=headers,
                    base_url="http://localhost",
                )
                self.assertEqual(
                    manifest_response.status_code,
                    200,
                    manifest_response.get_data(as_text=True),
                )
                manifest = manifest_response.get_json()
                art_manifest = manifest["files"]["mtg_art_hashes.db"]
                self.assertTrue(art_manifest["available"])
                self.assertEqual(art_manifest["row_count"], info["card_art"])
                self.assertEqual(art_manifest["phash_count"], info["phash"])

                download_response = client.get(
                    "/api/scanner/client-data/mtg_art_hashes.db",
                    headers=headers,
                    base_url="http://localhost",
                )
                self.assertEqual(download_response.status_code, 200)
                self.assertEqual(download_response.data, output_path.read_bytes())

        self.rollback_fixture(exercise)


if __name__ == "__main__":
    unittest.main()
