"""PostgreSQL cache operations for Scryfall card rulings."""

from __future__ import annotations

import json
from datetime import datetime


def fetch_cached_rulings(conn, scryfall_id: str):
    """Return a SQLite-compatible JSON-text cache row."""
    return conn.execute(
        """
        SELECT rulings::text AS rulings_json, fetched_at
        FROM catalog.printing_rulings_cache
        WHERE game_id = 'mtg'
          AND provider_id = 'scryfall'
          AND external_printing_id = ?
        """,
        (scryfall_id,),
    ).fetchone()


def save_cached_rulings(
    conn,
    scryfall_id: str,
    rulings: list[dict[str, str]],
    fetched_at: datetime,
) -> None:
    """Insert or refresh the provider-scoped rulings cache."""
    conn.execute(
        """
        INSERT INTO catalog.printing_rulings_cache AS cached (
            game_id,
            provider_id,
            external_printing_id,
            printing_id,
            rulings,
            fetched_at
        )
        VALUES (
            'mtg',
            'scryfall',
            ?,
            (
                SELECT e.printing_id
                FROM catalog.external_ids AS e
                WHERE e.game_id = 'mtg'
                  AND e.provider_id = 'scryfall'
                  AND e.entity_type = 'printing'
                  AND e.external_id_type = 'scryfall_id'
                  AND e.external_id = ?
                LIMIT 1
            ),
            ?::jsonb,
            ?
        )
        ON CONFLICT (game_id, provider_id, external_printing_id)
        DO UPDATE SET
            printing_id = COALESCE(EXCLUDED.printing_id, cached.printing_id),
            rulings = EXCLUDED.rulings,
            fetched_at = EXCLUDED.fetched_at
        """,
        (
            scryfall_id,
            scryfall_id,
            json.dumps(rulings, ensure_ascii=False),
            fetched_at,
        ),
    )
