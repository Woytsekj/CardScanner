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


    def test_catalog_tags_preserve_unresolved_illustrations(self):
        """Artwork assignments survive without inventing a printing face."""
        from uuid import uuid4

        from postgres_db import connection

        token = uuid4().hex

        class _RollbackFixture(Exception):
            pass

        with connection() as conn:
            try:
                with conn.transaction():
                    expected_tables = {
                        "tags", "tag_aliases", "tag_closure",
                        "art_tag_assignments", "oracle_tag_assignments",
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

                    # The source illustration is unresolved: create its identity
                    # and external mapping, but deliberately do not create a face.
                    illustration_id = conn.execute(
                        """
                        INSERT INTO catalog.illustrations (game_id)
                        VALUES ('mtg')
                        RETURNING illustration_id
                        """
                    ).fetchone()["illustration_id"]
                    source_illustration_id = f"legacy-illustration-{token}"
                    conn.execute(
                        """
                        INSERT INTO catalog.external_ids
                            (game_id, provider_id, entity_type, external_id_type,
                             external_id, illustration_id)
                        VALUES
                            ('mtg', 'scryfall', 'illustration', 'illustration_id',
                             %s, %s)
                        """,
                        (source_illustration_id, illustration_id),
                    )

                    tag_id = conn.execute(
                        """
                        INSERT INTO catalog.tags
                            (game_id, provider_id, external_tag_id, tag_type,
                             slug, label, parent_ids, child_ids)
                        VALUES
                            ('mtg', 'scryfall', %s, 'art', %s, %s,
                             '[]'::JSONB, '[]'::JSONB)
                        RETURNING tag_id
                        """,
                        (f"tag-{token}", f"fixture-art-{token}", f"Fixture Art {token}"),
                    ).fetchone()["tag_id"]

                    conn.execute(
                        """
                        INSERT INTO catalog.art_tag_assignments
                            (game_id, provider_id, tag_id, illustration_id,
                             weight, annotation)
                        VALUES ('mtg', 'scryfall', %s, %s, '0.75', 'legacy fixture')
                        """,
                        (tag_id, illustration_id),
                    )

                    face_count = conn.execute(
                        """
                        SELECT count(*) AS n
                        FROM catalog.printing_faces
                        WHERE illustration_id = %s
                        """,
                        (illustration_id,),
                    ).fetchone()["n"]
                    assignment = conn.execute(
                        """
                        SELECT t.external_tag_id, a.weight, a.annotation
                        FROM catalog.art_tag_assignments a
                        JOIN catalog.tags t USING (tag_id, game_id, provider_id)
                        WHERE a.illustration_id = %s
                        """,
                        (illustration_id,),
                    ).fetchone()

                    self.assertEqual(face_count, 0)
                    self.assertEqual(assignment["external_tag_id"], f"tag-{token}")
                    self.assertEqual(assignment["weight"], "0.75")
                    self.assertEqual(assignment["annotation"], "legacy fixture")

                    raise _RollbackFixture()
            except _RollbackFixture:
                pass

    def test_catalog_tag_aliases_are_case_insensitive_per_tag(self):
        """Aliases normalize case within one tag but may be shared by tags."""
        from uuid import uuid4

        from psycopg.errors import UniqueViolation

        from postgres_db import connection

        token = uuid4().hex

        class _RollbackFixture(Exception):
            pass

        with connection() as conn:
            try:
                with conn.transaction():
                    tag_ids = []
                    for suffix in ("one", "two"):
                        tag_ids.append(
                            conn.execute(
                                """
                                INSERT INTO catalog.tags
                                    (game_id, provider_id, external_tag_id,
                                     tag_type, slug, label)
                                VALUES ('mtg', 'scryfall', %s, 'art', %s, %s)
                                RETURNING tag_id
                                """,
                                (
                                    f"alias-{suffix}-{token}",
                                    f"alias-{suffix}-{token}",
                                    f"Alias Fixture {suffix} {token}",
                                ),
                            ).fetchone()["tag_id"]
                        )

                    alias = f"Shared Alias {token}"
                    conn.execute(
                        """
                        INSERT INTO catalog.tag_aliases
                            (tag_id, game_id, provider_id, alias)
                        VALUES (%s, 'mtg', 'scryfall', %s)
                        """,
                        (tag_ids[0], alias),
                    )

                    with self.assertRaises(UniqueViolation):
                        with conn.transaction():
                            conn.execute(
                                """
                                INSERT INTO catalog.tag_aliases
                                    (tag_id, game_id, provider_id, alias)
                                VALUES (%s, 'mtg', 'scryfall', %s)
                                """,
                                (tag_ids[0], alias.swapcase()),
                            )

                    # The old SQLite key was (tag_id, alias), not global alias.
                    conn.execute(
                        """
                        INSERT INTO catalog.tag_aliases
                            (tag_id, game_id, provider_id, alias)
                        VALUES (%s, 'mtg', 'scryfall', %s)
                        """,
                        (tag_ids[1], alias),
                    )

                    count = conn.execute(
                        """
                        SELECT count(*) AS n
                        FROM catalog.tag_aliases
                        WHERE lower(btrim(alias)) = lower(btrim(%s))
                        """,
                        (alias,),
                    ).fetchone()["n"]
                    self.assertEqual(count, 2)

                    raise _RollbackFixture()
            except _RollbackFixture:
                pass

    def test_catalog_tag_hierarchy_is_game_and_provider_scoped(self):
        """Closure relationships cannot cross a game's/provider's tag namespace."""
        from uuid import uuid4

        from psycopg.errors import ForeignKeyViolation

        from postgres_db import connection

        token = uuid4().hex

        class _RollbackFixture(Exception):
            pass

        with connection() as conn:
            try:
                with conn.transaction():
                    def insert_tag(game_id, provider_id, suffix):
                        return conn.execute(
                            """
                            INSERT INTO catalog.tags
                                (game_id, provider_id, external_tag_id,
                                 tag_type, slug, label)
                            VALUES (%s, %s, %s, 'art', %s, %s)
                            RETURNING tag_id
                            """,
                            (
                                game_id,
                                provider_id,
                                f"{game_id}-{suffix}-{token}",
                                f"{game_id}-{suffix}-{token}",
                                f"{game_id} {suffix} {token}",
                            ),
                        ).fetchone()["tag_id"]

                    parent_id = insert_tag("mtg", "scryfall", "parent")
                    child_id = insert_tag("mtg", "scryfall", "child")
                    pokemon_tag_id = insert_tag("pokemon", "tcgcsv", "pokemon")

                    conn.execute(
                        """
                        INSERT INTO catalog.tag_closure
                            (game_id, provider_id, tag_type, ancestor_tag_id,
                             descendant_tag_id, depth)
                        VALUES ('mtg', 'scryfall', 'art', %s, %s, 1)
                        """,
                        (parent_id, child_id),
                    )

                    with self.assertRaises(ForeignKeyViolation):
                        with conn.transaction():
                            conn.execute(
                                """
                                INSERT INTO catalog.tag_closure
                                    (game_id, provider_id, tag_type, ancestor_tag_id,
                                     descendant_tag_id, depth)
                                VALUES ('mtg', 'scryfall', 'art', %s, %s, 1)
                                """,
                                (parent_id, pokemon_tag_id),
                            )

                    raise _RollbackFixture()
            except _RollbackFixture:
                pass

    def test_oracle_tag_assignments_keep_unresolved_external_ids(self):
        """Oracle tag assignments retain provider IDs even before card resolution."""
        from uuid import uuid4

        from psycopg.errors import ForeignKeyViolation

        from postgres_db import connection

        token = uuid4().hex

        class _RollbackFixture(Exception):
            pass

        with connection() as conn:
            try:
                with conn.transaction():
                    tag_id = conn.execute(
                        """
                        INSERT INTO catalog.tags
                            (game_id, provider_id, external_tag_id, tag_type,
                             slug, label)
                        VALUES ('mtg', 'scryfall', %s, 'oracle', %s, %s)
                        RETURNING tag_id
                        """,
                        (
                            f"oracle-tag-{token}",
                            f"oracle-fixture-{token}",
                            f"Oracle Fixture {token}",
                        ),
                    ).fetchone()["tag_id"]

                    external_oracle_id = f"unresolved-oracle-{token}"
                    conn.execute(
                        """
                        INSERT INTO catalog.oracle_tag_assignments
                            (game_id, provider_id, tag_id, external_oracle_id,
                             weight, annotation)
                        VALUES ('mtg', 'scryfall', %s, %s, '1.0', 'unresolved')
                        """,
                        (tag_id, external_oracle_id),
                    )

                    unresolved = conn.execute(
                        """
                        SELECT external_oracle_id, card_id, weight, annotation
                        FROM catalog.oracle_tag_assignments
                        WHERE tag_id = %s AND external_oracle_id = %s
                        """,
                        (tag_id, external_oracle_id),
                    ).fetchone()
                    self.assertEqual(
                        unresolved,
                        {
                            "external_oracle_id": external_oracle_id,
                            "card_id": None,
                            "weight": "1.0",
                            "annotation": "unresolved",
                        },
                    )

                    illustration_id = conn.execute(
                        """
                        INSERT INTO catalog.illustrations (game_id)
                        VALUES ('mtg')
                        RETURNING illustration_id
                        """
                    ).fetchone()["illustration_id"]
                    with self.assertRaises(ForeignKeyViolation):
                        with conn.transaction():
                            conn.execute(
                                """
                                INSERT INTO catalog.art_tag_assignments
                                    (game_id, provider_id, tag_id, illustration_id)
                                VALUES ('mtg', 'scryfall', %s, %s)
                                """,
                                (tag_id, illustration_id),
                            )

                    pokemon_card_id = conn.execute(
                        """
                        INSERT INTO catalog.cards (game_id, name)
                        VALUES ('pokemon', %s)
                        RETURNING card_id
                        """,
                        (f"Wrong Game Card {token}",),
                    ).fetchone()["card_id"]

                    with self.assertRaises(ForeignKeyViolation):
                        with conn.transaction():
                            conn.execute(
                                """
                                INSERT INTO catalog.oracle_tag_assignments
                                    (game_id, provider_id, tag_id,
                                     external_oracle_id, card_id)
                                VALUES ('mtg', 'scryfall', %s, %s, %s)
                                """,
                                (tag_id, f"wrong-game-{token}", pokemon_card_id),
                            )

                    raise _RollbackFixture()
            except _RollbackFixture:
                pass



    def test_app_core_tables_and_legacy_compatibility(self):
        """Application data keeps legacy IDs, labels, and scan idempotency semantics."""
        from uuid import uuid4

        from psycopg.errors import ForeignKeyViolation, UniqueViolation

        from postgres_db import connection

        token = uuid4().hex

        class _RollbackFixture(Exception):
            pass

        with connection() as conn:
            try:
                with conn.transaction():
                    expected_tables = {
                        "people", "users", "login_attempts", "app_state",
                        "app_migrations", "inventory", "scan_log",
                        "decklists", "deck_cards",
                    }
                    rows = conn.execute(
                        """
                        SELECT table_name
                        FROM information_schema.tables
                        WHERE table_schema = 'app'
                          AND table_type = 'BASE TABLE'
                        """
                    ).fetchall()
                    self.assertTrue(
                        expected_tables.issubset({row["table_name"] for row in rows})
                    )

                    person_id = conn.execute(
                        """
                        INSERT INTO app.people (name)
                        VALUES (%s)
                        RETURNING id
                        """,
                        (f"Person {token}",),
                    ).fetchone()["id"]

                    with self.assertRaises(UniqueViolation):
                        with conn.transaction():
                            conn.execute(
                                "INSERT INTO app.people (name) VALUES (%s)",
                                (f"person {token}".swapcase(),),
                            )

                    conn.execute(
                        """
                        INSERT INTO app.users
                            (username, password_hash, person_id, is_admin, role)
                        VALUES (%s, %s, %s, TRUE, 'admin')
                        """,
                        (f"user-{token}", "test-password-hash", person_id),
                    )

                    with self.assertRaises(UniqueViolation):
                        with conn.transaction():
                            conn.execute(
                                """
                                INSERT INTO app.users
                                    (username, password_hash, person_id)
                                VALUES (%s, %s, %s)
                                """,
                                (f"second-user-{token}", "test-password-hash", person_id),
                            )

                    card_id = conn.execute(
                        """
                        INSERT INTO catalog.cards (game_id, name)
                        VALUES ('mtg', %s)
                        RETURNING card_id
                        """,
                        (f"Application Fixture Card {token}",),
                    ).fetchone()["card_id"]
                    printing_id = conn.execute(
                        """
                        INSERT INTO catalog.printings (game_id, card_id, collector_number)
                        VALUES ('mtg', %s, 'app-fixture')
                        RETURNING printing_id
                        """,
                        (card_id,),
                    ).fetchone()["printing_id"]

                    deck_id = conn.execute(
                        """
                        INSERT INTO app.decklists
                            (game_id, name, created_at, updated_at,
                             person_id, person_name, tags_json)
                        VALUES ('mtg', %s, now(), now(), %s, %s, '[]'::JSONB)
                        RETURNING id
                        """,
                        (f"Application Fixture Deck {token}", person_id, f"Person {token}"),
                    ).fetchone()["id"]

                    deck_card_id = conn.execute(
                        """
                        INSERT INTO app.deck_cards
                            (deck_id, game_id, name, quantity, card_id, printing_id)
                        VALUES (%s, 'mtg', 'Fixture Card', 1, %s, %s)
                        RETURNING id
                        """,
                        (deck_id, card_id, printing_id),
                    ).fetchone()["id"]

                    inventory_id = conn.execute(
                        """
                        INSERT INTO app.inventory
                            (game_id, card_id, printing_id, name, owner,
                             person_id, deck_id, location, for_vending)
                        VALUES ('mtg', %s, %s, 'Fixture Card',
                                %s, %s, %s, 'Binder', FALSE)
                        RETURNING id
                        """,
                        (card_id, printing_id, f"Person {token}", person_id, deck_id),
                    ).fetchone()["id"]

                    # A retry marker is intentionally valid even when its old
                    # inventory row no longer exists.
                    scan_id = f"scan-{token}"
                    conn.execute(
                        """
                        INSERT INTO app.scan_log (scan_id, game_id, row_id, quantity)
                        VALUES (%s, 'mtg', 922337203685477, 1)
                        """,
                        (scan_id,),
                    )
                    saved_scan = conn.execute(
                        "SELECT row_id, quantity FROM app.scan_log WHERE scan_id = %s",
                        (scan_id,),
                    ).fetchone()
                    self.assertEqual(
                        saved_scan,
                        {"row_id": 922337203685477, "quantity": 1},
                    )

                    pokemon_card_id = conn.execute(
                        """
                        INSERT INTO catalog.cards (game_id, name)
                        VALUES ('pokemon', %s)
                        RETURNING card_id
                        """,
                        (f"Wrong Game Fixture {token}",),
                    ).fetchone()["card_id"]
                    with self.assertRaises(ForeignKeyViolation):
                        with conn.transaction():
                            conn.execute(
                                """
                                INSERT INTO app.inventory
                                    (game_id, card_id, name)
                                VALUES ('mtg', %s, 'Cross-game card')
                                """,
                                (pokemon_card_id,),
                            )

                    # Removing a deck cascades its requirements and clears only
                    # the inventory allocation; the physical copy remains.
                    conn.execute("DELETE FROM app.decklists WHERE id = %s", (deck_id,))
                    deck_card_count = conn.execute(
                        "SELECT count(*) AS n FROM app.deck_cards WHERE id = %s",
                        (deck_card_id,),
                    ).fetchone()["n"]
                    inventory_after_deck_delete = conn.execute(
                        "SELECT deck_id, location, quantity FROM app.inventory WHERE id = %s",
                        (inventory_id,),
                    ).fetchone()
                    self.assertEqual(deck_card_count, 0)
                    self.assertEqual(
                        inventory_after_deck_delete,
                        {"deck_id": None, "location": "Binder", "quantity": 1},
                    )

                    # Person deletion unlinks identity but preserves the legacy
                    # owner/person-name snapshots and the login itself.
                    conn.execute("DELETE FROM app.people WHERE id = %s", (person_id,))
                    snapshots = conn.execute(
                        """
                        SELECT i.owner, i.person_id AS inventory_person_id,
                               u.person_id AS user_person_id
                        FROM app.inventory i
                        JOIN app.users u ON u.username = %s
                        WHERE i.id = %s
                        """,
                        (f"user-{token}", inventory_id),
                    ).fetchone()
                    self.assertEqual(snapshots["owner"], f"Person {token}")
                    self.assertIsNone(snapshots["inventory_person_id"])
                    self.assertIsNone(snapshots["user_person_id"])

                    raise _RollbackFixture()
            except _RollbackFixture:
                pass



if __name__ == "__main__":
    unittest.main()
