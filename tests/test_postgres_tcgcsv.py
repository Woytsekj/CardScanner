"""Integration tests for generic provider product and price catalog tables."""
from __future__ import annotations

import os
import unittest
from uuid import uuid4

from psycopg.errors import ForeignKeyViolation, UniqueViolation
from postgres_db import close_pool, connection, open_pool, run_migrations


@unittest.skipUnless(
    os.environ.get("CARD_SCANNER_POSTGRES_DSN"),
    "CARD_SCANNER_POSTGRES_DSN is not configured",
)
class PostgreSQLTCGCSVTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        open_pool()
        run_migrations()

    @classmethod
    def tearDownClass(cls):
        close_pool()

    def test_products_groups_and_price_observations_preserve_source_data(self):
        token = uuid4().hex

        class _RollbackFixture(Exception):
            pass

        with connection() as conn:
            try:
                with conn.transaction():
                    group_id = f"group-{token}"
                    product_id = f"product-{token}"
                    source_time = "2026-11-13T20:05:52+0000"

                    conn.execute(
                        """
                        INSERT INTO catalog.provider_product_groups
                            (game_id, provider_id, external_category_id,
                             external_group_id, name, abbreviation,
                             is_supplemental, published_on_source,
                             modified_on_source, source_payload)
                        VALUES
                            ('mtg', 'tcgcsv', '1', %s, 'Fixture Set',
                             'FIX', FALSE, '2026-01-01T00:00:00',
                             '2026-11-12T14:36:46.133',
                             '{"categoryId":1}'::JSONB)
                        """,
                        (group_id,),
                    )

                    # The group's category must match the configured game/provider category.
                    with self.assertRaises(ForeignKeyViolation):
                        with conn.transaction():
                            conn.execute(
                                """
                                INSERT INTO catalog.provider_product_groups
                                    (game_id, provider_id, external_category_id,
                                     external_group_id, name, source_payload)
                                VALUES
                                    ('pokemon', 'tcgcsv', '1', %s,
                                     'Wrong Category', '{}'::JSONB)
                                """,
                                (f"wrong-category-{token}",),
                            )

                    conn.execute(
                        """
                        INSERT INTO catalog.provider_products
                            (game_id, provider_id, external_product_id,
                             external_category_id, external_group_id,
                             name, clean_name, product_url, image_url,
                             image_count, modified_on_source, presale_info,
                             extended_data, source_payload)
                        VALUES
                            ('mtg', 'tcgcsv', %s, '1', %s,
                             'Fixture Card', 'Fixture Card',
                             'https://example.invalid/product',
                             'https://example.invalid/image.jpg',
                             1, '2026-11-12T14:36:46.133',
                             '{"isPresale":false}'::JSONB,
                             '[{"name":"Rarity","value":"R"}]'::JSONB,
                             '{"productId":123}'::JSONB)
                        """,
                        (product_id, group_id),
                    )

                    product = conn.execute(
                        """
                        SELECT name, extended_data, presale_info
                        FROM catalog.provider_products
                        WHERE game_id = 'mtg'
                          AND provider_id = 'tcgcsv'
                          AND external_product_id = %s
                        """,
                        (product_id,),
                    ).fetchone()

                    self.assertEqual(product["name"], "Fixture Card")
                    self.assertEqual(
                        product["extended_data"],
                        [{"name": "Rarity", "value": "R"}],
                    )
                    self.assertEqual(
                        product["presale_info"], {"isPresale": False}
                    )

                    # Preserve all price fields and the exact source subtype.
                    conn.execute(
                        """
                        INSERT INTO catalog.provider_price_observations
                            (game_id, provider_id, external_product_id,
                             subtype_name, source_updated, market_price,
                             low_price, mid_price, high_price, direct_low_price)
                        VALUES
                            ('mtg', 'tcgcsv', %s, 'Normal', %s,
                             1.25, 1.00, 1.10, 1.50, NULL)
                        """,
                        (product_id, source_time),
                    )

                    observation = conn.execute(
                        """
                        SELECT subtype_name, source_updated, market_price,
                               low_price, mid_price, high_price, direct_low_price
                        FROM catalog.provider_price_observations
                        WHERE external_product_id = %s
                        """,
                        (product_id,),
                    ).fetchone()

                    self.assertEqual(observation["subtype_name"], "Normal")
                    self.assertEqual(observation["source_updated"], source_time)
                    self.assertEqual(str(observation["market_price"]), "1.25")
                    self.assertIsNone(observation["direct_low_price"])

                    # A different subtype is a distinct source observation.
                    conn.execute(
                        """
                        INSERT INTO catalog.provider_price_observations
                            (game_id, provider_id, external_product_id,
                             subtype_name, source_updated)
                        VALUES
                            ('mtg', 'tcgcsv', %s, 'Foil', %s)
                        """,
                        (product_id, source_time),
                    )

                    # Different source text remains distinct even for the same instant.
                    conn.execute(
                        """
                        INSERT INTO catalog.provider_price_observations
                            (game_id, provider_id, external_product_id,
                             subtype_name, source_updated)
                        VALUES
                            ('mtg', 'tcgcsv', %s, 'Normal',
                             '2026-11-13T20:05:52+00:00')
                        """,
                        (product_id,),
                    )

                    with self.assertRaises(UniqueViolation):
                        with conn.transaction():
                            conn.execute(
                                """
                                INSERT INTO catalog.provider_price_observations
                                    (game_id, provider_id, external_product_id,
                                     subtype_name, source_updated)
                                VALUES
                                    ('mtg', 'tcgcsv', %s, 'Normal', %s)
                                """,
                                (product_id, source_time),
                            )

                    # Price observations may remain when product metadata is unresolved.
                    conn.execute(
                        """
                        INSERT INTO catalog.provider_price_observations
                            (game_id, provider_id, external_product_id,
                             subtype_name, source_updated)
                        VALUES
                            ('pokemon', 'tcgcsv', %s, 'Holofoil', %s)
                        """,
                        (f"unresolved-{token}", source_time),
                    )

                    # A product cannot reference a group in another game.
                    with self.assertRaises(ForeignKeyViolation):
                        with conn.transaction():
                            conn.execute(
                                """
                                INSERT INTO catalog.provider_products
                                    (game_id, provider_id, external_product_id,
                                     external_category_id, external_group_id,
                                     name, source_payload)
                                VALUES
                                    ('pokemon', 'tcgcsv', %s, '3', %s,
                                     'Wrong Group', '{}'::JSONB)
                                """,
                                (f"wrong-game-{token}", group_id),
                            )

                    raise _RollbackFixture()
            except _RollbackFixture:
                pass


if __name__ == "__main__":
    unittest.main()
