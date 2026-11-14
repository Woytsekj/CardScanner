"""Integration tests for the isolated PostgreSQL foundation.

Run against a disposable development/test database with
CARD_SCANNER_POSTGRES_DSN configured. These tests never touch SQLite.
"""
from __future__ import annotations

import os
import unittest

from postgres_db import close_pool, open_pool, run_migrations


@unittest.skipUnless(
    os.environ.get("CARD_SCANNER_POSTGRES_DSN"),
    "CARD_SCANNER_POSTGRES_DSN is not configured",
)
class PostgreSQLFoundationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        open_pool()
        run_migrations()

    @classmethod
    def tearDownClass(cls):
        close_pool()

    def test_initial_games_and_tcgcsv_categories(self):
        from postgres_db import connection

        with connection() as conn:
            rows = conn.execute(
                """
                SELECT g.game_id, gp.provider_category_id
                FROM catalog.games AS g
                JOIN catalog.game_providers AS gp USING (game_id)
                WHERE gp.provider_id = 'tcgcsv'
                ORDER BY g.game_id
                """
            ).fetchall()

        self.assertEqual(
            rows,
            [
                {"game_id": "mtg", "provider_category_id": "1"},
                {"game_id": "one_piece", "provider_category_id": "68"},
                {"game_id": "pokemon", "provider_category_id": "3"},
                {"game_id": "riftbound", "provider_category_id": "89"},
            ],
        )

    def test_migrations_are_idempotent(self):
        self.assertEqual(run_migrations(), [])

    def test_scryfall_is_only_registered_for_mtg(self):
        from postgres_db import connection

        with connection() as conn:
            rows = conn.execute(
                "SELECT game_id FROM catalog.game_providers WHERE provider_id = 'scryfall'"
            ).fetchall()

        self.assertEqual(rows, [{"game_id": "mtg"}])


    def test_catalog_core_supports_game_scoped_catalog_and_media(self):
        """Catalog identities, media, hashes, and provider IDs remain game-scoped."""
        from uuid import uuid4

        from psycopg.errors import ForeignKeyViolation, UniqueViolation

        from postgres_db import connection

        token = uuid4().hex
        class _RollbackFixture(Exception):
            pass

        with connection() as conn:
            try:
                # Keep this integration fixture entirely transactional.
                with conn.transaction():
                    expected_tables = {
                        "sets", "cards", "printings", "printing_faces",
                        "illustrations", "images", "image_hashes", "external_ids",
                        "game_catalog_state",
                    }
                    rows = conn.execute(
                        """
                        SELECT table_name
                        FROM information_schema.tables
                        WHERE table_schema = 'catalog'
                          AND table_type = 'BASE TABLE'
                        """
                    ).fetchall()
                    self.assertTrue(
                        expected_tables.issubset({row["table_name"] for row in rows})
                    )

                    set_id = conn.execute(
                        """
                        INSERT INTO catalog.sets (game_id, name, set_code)
                        VALUES ('mtg', %s, %s)
                        RETURNING set_id
                        """,
                        (f"Fixture Set {token}", f"t{token[:10]}"),
                    ).fetchone()["set_id"]

                    card_id = conn.execute(
                        """
                        INSERT INTO catalog.cards (game_id, name, type_line)
                        VALUES ('mtg', %s, 'Creature')
                        RETURNING card_id
                        """,
                        (f"Fixture Card {token}",),
                    ).fetchone()["card_id"]

                    # An illustration is allowed to exist before a printing/face
                    # references it; this is needed for legacy unresolved IDs.
                    illustration_id = conn.execute(
                        """
                        INSERT INTO catalog.illustrations (game_id)
                        VALUES ('mtg')
                        RETURNING illustration_id
                        """
                    ).fetchone()["illustration_id"]

                    unresolved_illustration_id = conn.execute(
                        """
                        INSERT INTO catalog.illustrations (game_id)
                        VALUES ('mtg')
                        RETURNING illustration_id
                        """
                    ).fetchone()["illustration_id"]

                    image_id = conn.execute(
                        """
                        INSERT INTO catalog.images
                            (game_id, provider_id, image_uri, image_role, media_type)
                        VALUES
                            ('mtg', 'scryfall',
                             %s, 'card_face', 'image/jpeg')
                        RETURNING image_id
                        """,
                        (f"https://fixture.invalid/{token}.jpg",),
                    ).fetchone()["image_id"]

                    conn.execute(
                        """
                        INSERT INTO catalog.image_hashes
                            (image_id, game_id, hash_algorithm, hash_version, hash_value)
                        VALUES (%s, 'mtg', 'phash', '1', %s)
                        """,
                        (image_id, f"fixture-hash-{token}"),
                    )

                    printing_id = conn.execute(
                        """
                        INSERT INTO catalog.printings
                            (game_id, card_id, set_id, collector_number,
                             language_code, rarity, available_finishes, image_id)
                        VALUES
                            ('mtg', %s, %s, '0175/281', 'en', 'rare',
                             ARRAY['nonfoil', 'foil']::TEXT[], %s)
                        RETURNING printing_id
                        """,
                        (card_id, set_id, image_id),
                    ).fetchone()["printing_id"]

                    conn.execute(
                        """
                        INSERT INTO catalog.printing_faces
                            (printing_id, game_id, face_index, name,
                             image_id, illustration_id)
                        VALUES (%s, 'mtg', 0, 'Fixture Card', %s, %s)
                        """,
                        (printing_id, image_id, illustration_id),
                    )

                    conn.execute(
                        """
                        INSERT INTO catalog.external_ids
                            (game_id, provider_id, entity_type, external_id_type,
                             external_id, card_id)
                        VALUES ('mtg', 'scryfall', 'card', 'oracle_id', %s, %s)
                        """,
                        (f"oracle-{token}", card_id),
                    )
                    external_printing_id = f"scryfall-{token}"
                    conn.execute(
                        """
                        INSERT INTO catalog.external_ids
                            (game_id, provider_id, entity_type, external_id_type,
                             external_id, printing_id)
                        VALUES ('mtg', 'scryfall', 'printing', 'scryfall_id', %s, %s)
                        """,
                        (external_printing_id, printing_id),
                    )
                    conn.execute(
                        """
                        INSERT INTO catalog.external_ids
                            (game_id, provider_id, entity_type, external_id_type,
                             external_id, illustration_id)
                        VALUES ('mtg', 'scryfall', 'illustration',
                                'illustration_id', %s, %s)
                        """,
                        (f"illustration-{token}", illustration_id),
                    )

                    unresolved_face_count = conn.execute(
                        """
                        SELECT count(*) AS n
                        FROM catalog.printing_faces
                        WHERE illustration_id = %s
                        """,
                        (unresolved_illustration_id,),
                    ).fetchone()["n"]
                    self.assertEqual(unresolved_face_count, 0)

                    # The provider ID namespace is unique across entity targets.
                    another_printing_id = conn.execute(
                        """
                        INSERT INTO catalog.printings (game_id, collector_number)
                        VALUES ('mtg', 'fixture-duplicate')
                        RETURNING printing_id
                        """
                    ).fetchone()["printing_id"]
                    with self.assertRaises(UniqueViolation):
                        with conn.transaction():
                            conn.execute(
                                """
                                INSERT INTO catalog.external_ids
                                    (game_id, provider_id, entity_type,
                                     external_id_type, external_id, printing_id)
                                VALUES ('mtg', 'scryfall', 'printing',
                                        'scryfall_id', %s, %s)
                                """,
                                (external_printing_id, another_printing_id),
                            )

                    # A card from Pokémon cannot be attached to an MTG printing.
                    pokemon_card_id = conn.execute(
                        """
                        INSERT INTO catalog.cards (game_id, name)
                        VALUES ('pokemon', %s)
                        RETURNING card_id
                        """,
                        (f"Fixture Pokémon {token}",),
                    ).fetchone()["card_id"]
                    with self.assertRaises(ForeignKeyViolation):
                        with conn.transaction():
                            conn.execute(
                                """
                                INSERT INTO catalog.printings (game_id, card_id)
                                VALUES ('mtg', %s)
                                """,
                                (pokemon_card_id,),
                            )

                    # Deleting an image nulls only the optional image_id columns,
                    # preserving each row's non-null game_id. Hashes cascade away.
                    conn.execute(
                        "DELETE FROM catalog.images WHERE image_id = %s",
                        (image_id,),
                    )
                    printing = conn.execute(
                        """
                        SELECT game_id, image_id
                        FROM catalog.printings WHERE printing_id = %s
                        """,
                        (printing_id,),
                    ).fetchone()
                    face = conn.execute(
                        """
                        SELECT game_id, image_id, illustration_id
                        FROM catalog.printing_faces
                        WHERE printing_id = %s AND face_index = 0
                        """,
                        (printing_id,),
                    ).fetchone()
                    hash_count = conn.execute(
                        "SELECT count(*) AS n FROM catalog.image_hashes WHERE image_id = %s",
                        (image_id,),
                    ).fetchone()["n"]

                    self.assertEqual(printing, {"game_id": "mtg", "image_id": None})
                    self.assertEqual(
                        face,
                        {
                            "game_id": "mtg",
                            "image_id": None,
                            "illustration_id": illustration_id,
                        },
                    )
                    self.assertEqual(hash_count, 0)

                    raise _RollbackFixture()
            except _RollbackFixture:
                pass

    def test_catalog_core_allows_printings_without_known_card_or_set(self):
        """Games without a resolvable canonical card/set can still store printings."""
        from uuid import uuid4

        from postgres_db import connection

        token = uuid4().hex
        class _RollbackFixture(Exception):
            pass

        with connection() as conn:
            try:
                with conn.transaction():
                    row = conn.execute(
                        """
                        INSERT INTO catalog.printings
                            (game_id, collector_number, language_code, metadata)
                        VALUES ('one_piece', %s, 'en', '{"source":"fixture"}'::JSONB)
                        RETURNING game_id, card_id, set_id, collector_number
                        """,
                        (f"unknown-{token}",),
                    ).fetchone()
                    self.assertEqual(
                        row,
                        {
                            "game_id": "one_piece",
                            "card_id": None,
                            "set_id": None,
                            "collector_number": f"unknown-{token}",
                        },
                    )
                    raise _RollbackFixture()
            except _RollbackFixture:
                pass



if __name__ == "__main__":
    unittest.main()
