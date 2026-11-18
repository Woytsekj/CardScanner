"""PostgreSQL read queries for MTG deck pages and inventory comparison."""

from __future__ import annotations

from typing import Any


def fetch_active_people(conn) -> list[str]:
    """Return active collection owners in case-insensitive display order."""
    rows = conn.execute(
        """
        SELECT name
        FROM app.people
        WHERE active IS TRUE
        ORDER BY LOWER(name), name
        """
    ).fetchall()
    return [row["name"] for row in rows]


def fetch_deck_list_data(conn):
    """Load MTG deck summaries, active owners, and inventory locations."""
    rows = conn.execute(
        """
        SELECT
            d.*,
            COALESCE(SUM(dc.quantity), 0)::bigint AS card_count
        FROM app.decklists AS d
        LEFT JOIN app.deck_cards AS dc
          ON dc.deck_id = d.id
         AND dc.game_id = d.game_id
        WHERE d.game_id = 'mtg'
        GROUP BY d.id
        ORDER BY d.updated_at DESC, LOWER(d.name), d.name
        """
    ).fetchall()

    owners = fetch_active_people(conn)

    locations = [
        row["location"]
        for row in conn.execute(
            """
            SELECT location
            FROM (
                SELECT DISTINCT COALESCE(location, 'Free') AS location
                FROM app.inventory
                WHERE game_id = 'mtg'
            ) AS inventory_locations
            ORDER BY LOWER(location), location
            """
        ).fetchall()
    ]

    return rows, owners, locations


def fetch_deck_detail_data(conn, deck_id: int):
    """Load one MTG deck and its card rows without mutating the deck."""
    deck = conn.execute(
        """
        SELECT *
        FROM app.decklists
        WHERE id = ?
          AND game_id = 'mtg'
        """,
        (deck_id,),
    ).fetchone()

    owners = fetch_active_people(conn)
    if not deck:
        return None, owners, []

    cards = conn.execute(
        """
        SELECT dc.*
        FROM app.deck_cards AS dc
        WHERE dc.deck_id = ?
          AND dc.game_id = 'mtg'
        ORDER BY
            CASE
                WHEN LOWER(COALESCE(dc.category, '')) = 'commander'
                THEN 0 ELSE 1
            END,
            LOWER(COALESCE(dc.category, '')),
            LOWER(dc.name),
            dc.id
        """,
        (deck_id,),
    ).fetchall()

    return deck, owners, cards


def fetch_deck_comparison_data(conn, deck_id: int):
    """Load comparison inputs with Oracle IDs and TCGCSV mappings resolved."""
    deck = conn.execute(
        """
        SELECT name, person_name
        FROM app.decklists
        WHERE id = ?
          AND game_id = 'mtg'
        """,
        (deck_id,),
    ).fetchone()

    if not deck:
        return None, [], []

    cards = conn.execute(
        """
        SELECT
            dc.id,
            dc.name,
            dc.quantity,
            dc.scryfall_id,
            dc.set_code,
            dc.collector_number,
            dc.category,
            dc.finish,
            dc.any_printing,
            COALESCE(
                NULLIF(dc.tcgplayer_id, ''),
                NULLIF(tcg.target_external_product_id, ''),
                NULLIF(p.metadata->>'tcgplayer_id', '')
            ) AS resolved_tcgplayer_id,
            dc.role_tags_json,
            COALESCE(
                NULLIF(c.metadata->>'oracle_id', ''),
                NULLIF(p.metadata->>'oracle_id', '')
            ) AS card_oracle_id
        FROM app.deck_cards AS dc
        LEFT JOIN catalog.external_ids AS e
          ON e.game_id = dc.game_id
         AND e.provider_id = 'scryfall'
         AND e.entity_type = 'printing'
         AND e.external_id_type = 'scryfall_id'
         AND e.external_id = dc.scryfall_id
        LEFT JOIN catalog.printings AS p
          ON p.game_id = dc.game_id
         AND p.printing_id = COALESCE(dc.printing_id, e.printing_id)
        LEFT JOIN catalog.cards AS c
          ON c.game_id = dc.game_id
         AND c.card_id = COALESCE(dc.card_id, p.card_id)
        LEFT JOIN LATERAL (
            SELECT m.target_external_product_id
            FROM catalog.printing_product_mappings AS m
            WHERE m.game_id = dc.game_id
              AND m.printing_id = COALESCE(dc.printing_id, p.printing_id)
              AND m.source_provider_id = 'scryfall'
              AND m.target_provider_id = 'tcgcsv'
            ORDER BY m.target_external_product_id
            LIMIT 1
        ) AS tcg ON TRUE
        WHERE dc.deck_id = ?
          AND dc.game_id = 'mtg'
        ORDER BY
            CASE
                WHEN LOWER(COALESCE(dc.category, '')) = 'commander'
                THEN 0 ELSE 1
            END,
            LOWER(COALESCE(dc.category, '')),
            LOWER(dc.name),
            dc.id
        """,
        (deck_id,),
    ).fetchall()

    inventory = conn.execute(
        """
        SELECT
            i.id,
            i.scryfall_id,
            i.name,
            i.set_code,
            i.collector_number,
            i.finish,
            i.owner,
            i.quantity,
            i.deck_id,
            i.for_vending,
            COALESCE(i.location, 'Free') AS location,
            i.tcgplayer_id,
            COALESCE(
                NULLIF(c.metadata->>'oracle_id', ''),
                NULLIF(p.metadata->>'oracle_id', '')
            ) AS oracle_id,
            d.name AS assigned_deck_name
        FROM app.inventory AS i
        LEFT JOIN catalog.external_ids AS e
          ON e.game_id = i.game_id
         AND e.provider_id = 'scryfall'
         AND e.entity_type = 'printing'
         AND e.external_id_type = 'scryfall_id'
         AND e.external_id = i.scryfall_id
        LEFT JOIN catalog.printings AS p
          ON p.game_id = i.game_id
         AND p.printing_id = COALESCE(i.printing_id, e.printing_id)
        LEFT JOIN catalog.cards AS c
          ON c.game_id = i.game_id
         AND c.card_id = COALESCE(i.card_id, p.card_id)
        LEFT JOIN app.decklists AS d
          ON d.id = i.deck_id
         AND d.game_id = i.game_id
        WHERE i.game_id = 'mtg'
        ORDER BY i.id
        """
    ).fetchall()

    return deck, cards, inventory
