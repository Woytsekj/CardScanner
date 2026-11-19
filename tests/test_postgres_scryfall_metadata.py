"""PostgreSQL integration tests for Scryfall metadata enrichment and bulk refresh."""

from __future__ import annotations

import gzip
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from postgres_app_db import connect_app_database
from postgres_db import close_pool, open_pool, run_migrations
from postgres_scryfall_metadata import (
    fetch_missing_scryfall_ids,
    fetch_owned_scryfall_ids,
    save_provider_printing_metadata,
)


class FakeResponse:
    def __init__(self, *, payload=None, chunks=None):
        self.payload = payload
        self.chunks = list(chunks or [])

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload

    def iter_content(self, chunk_size):
        yield from self.chunks

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


@unittest.skipUnless(
    os.environ.get("CARD_SCANNER_POSTGRES_DSN")
    and os.environ.get("CARD_SCANNER_DATABASE_BACKEND") == "postgres",
    "Run with the PostgreSQL backend and development DSN configured",
)
class PostgreSQLScryfallMetadataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        open_pool()
        run_migrations()

        import app as app_module

        if app_module.DATABASE_BACKEND != "postgres":
            raise RuntimeError("Scryfall metadata tests require PostgreSQL app mode")
        cls.app_module = app_module
        cls.app_module.app.config["TESTING"] = True

    @classmethod
    def tearDownClass(cls):
        close_pool()

    def setUp(self):
        self.token = uuid4().hex
        self.scryfall_id = str(uuid4())
        self.username = f"pg-scryfall-admin-{self.token}"
        self.csrf = f"csrf-scryfall-{self.token}"
        self.card_name = f"PG Scryfall Card {self.token}"

        self.temp = tempfile.TemporaryDirectory(prefix="cardscanner-pg-scryfall-")
        self.data_dir = Path(self.temp.name)
        self.bulk_meta_path = self.data_dir / "scryfall_bulk_sync.json"

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
                (self.username, "unused-scryfall-test-password-hash"),
            )
            conn.execute(
                """
                INSERT INTO app.inventory (
                    game_id, scryfall_id, name, quantity, owner,
                    finish, location, for_vending
                )
                VALUES ('mtg', ?, ?, 1, 'PG Scryfall Test', 'normal', 'Free', FALSE)
                """,
                (self.scryfall_id, self.card_name),
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
            patch.object(self.app_module, "DATA_DIR", self.data_dir),
            patch.object(self.app_module, "SCRYFALL_BULK_META_FILE", self.bulk_meta_path),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()

        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            conn.execute(
                "DELETE FROM app.card_aliases WHERE external_printing_id = ?",
                (self.scryfall_id,),
            )
            conn.execute(
                "DELETE FROM catalog.printing_product_mappings "
                "WHERE source_external_printing_id = ?",
                (self.scryfall_id,),
            )
            conn.execute(
                "DELETE FROM catalog.provider_printing_metadata "
                "WHERE external_printing_id = ?",
                (self.scryfall_id,),
            )
            conn.execute(
                "DELETE FROM app.inventory WHERE scryfall_id = ?",
                (self.scryfall_id,),
            )
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

        self.temp.cleanup()

    def card_payload(self, *, name=None):
        return {
            "id": self.scryfall_id,
            "oracle_id": str(uuid4()),
            "name": name or self.card_name,
            "printed_name": "Carta de prueba",
            "type_line": "Creature — Test",
            "colors": ["G"],
            "color_identity": ["G"],
            "mana_value": 2,
            "mana_cost": "{1}{G}",
            "oracle_text": "When this creature enters the battlefield, draw a card.",
            "legalities": {"commander": "legal", "standard": "not_legal"},
            "tcgplayer_id": 246801,
            "finishes": ["nonfoil", "foil"],
            "set": "tst",
            "collector_number": "42",
        }

    def message(self, response):
        return parse_qs(urlparse(response.headers["Location"]).query).get(
            "message", [""]
        )[0]

    def fetch_saved_metadata(self):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            metadata = conn.execute(
                """
                SELECT external_printing_id, printing_id, name, type_line,
                       colors, colors_text, color_identity, mana_value,
                       mana_cost, oracle_text, external_oracle_id, legalities
                FROM catalog.provider_printing_metadata
                WHERE game_id = 'mtg'
                  AND provider_id = 'scryfall'
                  AND external_printing_id = ?
                """,
                (self.scryfall_id,),
            ).fetchone()
            mapping = conn.execute(
                """
                SELECT target_external_product_id, finishes, card_name,
                       set_code, collector_number
                FROM catalog.printing_product_mappings
                WHERE game_id = 'mtg'
                  AND source_provider_id = 'scryfall'
                  AND source_external_printing_id = ?
                  AND target_provider_id = 'tcgcsv'
                """,
                (self.scryfall_id,),
            ).fetchone()
            return metadata, mapping
        finally:
            conn.close()

    def test_provider_metadata_and_price_mapping_upsert_without_catalog_printing(self):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            self.assertEqual(fetch_owned_scryfall_ids(conn), [self.scryfall_id])
            self.assertEqual(fetch_missing_scryfall_ids(conn, 10), [self.scryfall_id])

            payload = self.card_payload()
            save_provider_printing_metadata(conn, self.scryfall_id, payload)
            conn.commit()

            metadata = conn.execute(
                """
                SELECT printing_id, name, type_line, colors, colors_text,
                       color_identity, mana_value, mana_cost, oracle_text,
                       external_oracle_id, legalities
                FROM catalog.provider_printing_metadata
                WHERE game_id = 'mtg' AND provider_id = 'scryfall'
                  AND external_printing_id = ?
                """,
                (self.scryfall_id,),
            ).fetchone()
            self.assertIsNone(metadata["printing_id"])
            self.assertEqual(metadata["name"], self.card_name)
            self.assertEqual(metadata["type_line"], "Creature — Test")
            self.assertEqual(metadata["colors"], ["G"])
            self.assertEqual(metadata["colors_text"], "G")
            self.assertEqual(metadata["color_identity"], ["G"])
            self.assertEqual(float(metadata["mana_value"]), 2.0)
            self.assertEqual(metadata["mana_cost"], "{1}{G}")
            self.assertIn("draw a card", metadata["oracle_text"])
            self.assertEqual(metadata["legalities"]["commander"], "legal")

            mapping = conn.execute(
                """
                SELECT target_external_product_id, finishes, card_name,
                       set_code, collector_number
                FROM catalog.printing_product_mappings
                WHERE game_id = 'mtg'
                  AND source_provider_id = 'scryfall'
                  AND source_external_printing_id = ?
                  AND target_provider_id = 'tcgcsv'
                """,
                (self.scryfall_id,),
            ).fetchone()
            self.assertEqual(mapping["target_external_product_id"], "246801")
            self.assertEqual(mapping["finishes"], ["nonfoil", "foil"])
            self.assertEqual(mapping["card_name"], self.card_name)
            self.assertEqual(mapping["set_code"], "tst")
            self.assertEqual(mapping["collector_number"], "42")

            aliases = conn.execute(
                """
                SELECT alias FROM app.card_aliases
                WHERE game_id = 'mtg' AND provider_id = 'scryfall'
                  AND external_printing_id = ?
                """,
                (self.scryfall_id,),
            ).fetchall()
            self.assertEqual([row["alias"] for row in aliases], ["Carta de prueba"])
            self.assertEqual(fetch_missing_scryfall_ids(conn, 10), [])

            # Re-import updates the metadata and mapping without duplicating rows.
            changed = self.card_payload(name="Updated Test Card")
            changed["tcgplayer_id"] = "13579"
            changed["colors"] = ["R"]
            save_provider_printing_metadata(conn, self.scryfall_id, changed)
            conn.commit()

            count = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM catalog.provider_printing_metadata
                WHERE game_id = 'mtg' AND provider_id = 'scryfall'
                  AND external_printing_id = ?
                """,
                (self.scryfall_id,),
            ).fetchone()["count"]
            self.assertEqual(int(count), 1)
            updated = conn.execute(
                """
                SELECT name, colors_text
                FROM catalog.provider_printing_metadata
                WHERE game_id = 'mtg' AND provider_id = 'scryfall'
                  AND external_printing_id = ?
                """,
                (self.scryfall_id,),
            ).fetchone()
            self.assertEqual(updated["name"], "Updated Test Card")
            self.assertEqual(updated["colors_text"], "R")
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def test_bulk_refresh_downloads_compressed_default_cards_and_persists_metadata(self):
        payload = self.card_payload()
        compressed = gzip.compress((json.dumps(payload) + "\n").encode("utf-8"))
        manifest = {
            "data": [{
                "type": "default_cards",
                "name": "Default Cards",
                "updated_at": "2026-11-18T12:00:00Z",
                "download_uri": "https://fixture.invalid/default-cards.jsonl.gz",
            }]
        }
        responses = [
            FakeResponse(payload=manifest),
            FakeResponse(chunks=[compressed]),
        ]

        with patch.object(
            self.app_module.requests,
            "get",
            side_effect=responses,
        ) as get:
            response = self.client.post(
                "/admin/refresh_scryfall_bulk",
                data={"csrf_token": self.csrf},
                base_url="http://localhost",
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn("1 of 1 owned printings updated", self.message(response))
        self.assertEqual(get.call_count, 2)

        metadata, mapping = self.fetch_saved_metadata()
        self.assertIsNotNone(metadata)
        self.assertEqual(metadata["name"], self.card_name)
        self.assertEqual(mapping["target_external_product_id"], "246801")
        saved_meta = json.loads(self.bulk_meta_path.read_text(encoding="utf-8"))
        self.assertEqual(saved_meta["found"], 1)
        self.assertEqual(saved_meta["needed"], 1)
        self.assertEqual(saved_meta["updated_at"], "2026-11-18T12:00:00Z")

    def test_individual_enrichment_fetches_only_missing_owned_metadata(self):
        payload = self.card_payload()
        with (
            patch.object(
                self.app_module.requests,
                "get",
                return_value=FakeResponse(payload=payload),
            ) as get,
            patch.object(self.app_module.time, "sleep", return_value=None),
        ):
            response = self.client.post(
                "/admin/enrich_metadata",
                data={"csrf_token": self.csrf, "limit": "25"},
                base_url="http://localhost",
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn("1 of 1 cards updated", self.message(response))
        get.assert_called_once()
        metadata, mapping = self.fetch_saved_metadata()
        self.assertIsNotNone(metadata)
        self.assertEqual(mapping["target_external_product_id"], "246801")

        card_info = self.client.get(
            f"/api/card-info/{self.scryfall_id}",
            base_url="http://localhost",
        )
        self.assertEqual(card_info.status_code, 200, card_info.get_data(as_text=True))
        card_payload = card_info.get_json()["card"]
        self.assertEqual(card_payload["name"], self.card_name)
        self.assertEqual(card_payload["colors"], "G")
        self.assertEqual(card_payload["legalities"]["commander"], "legal")

        with (
            patch.object(self.app_module.requests, "get") as no_second_fetch,
            patch.object(self.app_module.time, "sleep", return_value=None),
        ):
            second = self.client.post(
                "/admin/enrich_metadata",
                data={"csrf_token": self.csrf, "limit": "25"},
                base_url="http://localhost",
            )
        self.assertEqual(second.status_code, 302)
        self.assertIn("0 of 0 cards updated", self.message(second))
        no_second_fetch.assert_not_called()

    def test_metadata_refresh_routes_reject_missing_login_and_invalid_csrf(self):
        guest = self.app_module.app.test_client()
        guest_response = guest.post(
            "/admin/enrich_metadata",
            data={"limit": "1"},
            base_url="http://localhost",
        )
        self.assertEqual(guest_response.status_code, 401)

        bad_csrf = self.client.post(
            "/admin/refresh_scryfall_bulk",
            data={"csrf_token": "wrong-token"},
            base_url="http://localhost",
        )
        self.assertEqual(bad_csrf.status_code, 400)


if __name__ == "__main__":
    unittest.main()
