"""Read-only PostgreSQL queries for the manual-add card catalog search."""

from __future__ import annotations


def _scryfall_image_url(scryfall_id: str | None) -> str | None:
    if not scryfall_id:
        return None

    sid = str(scryfall_id).strip().lower()
    if len(sid) < 2:
        return None

    return (
        "https://cards.scryfall.io/normal/front/"
        f"{sid[0]}/{sid[1]}/{sid}.jpg"
    )


def _normalize_tcgplayer_id(value):
    """Preserve the legacy SQLite INTEGER response for numeric product IDs."""
    if value is None:
        return None

    value = str(value).strip()
    if value.isdigit():
        return int(value)

    return value or None


def fetch_catalog_search_results(
    conn,
    query: str,
    limit: int = 12,
) -> list[dict]:
    """Search MTG printings using the legacy manual-add search behavior.

    The caller owns the connection. Search matches card names, set codes, and
    collector numbers. Names fall back to printing metadata for records that
    do not have a linked catalog.cards row.
    """
    if not str(query or "").strip():
        return []

    search_text = str(query).strip()[:120].replace("%", "").replace("_", "")
    if not search_text and str(query).strip():
        # Legacy LIKE patterns become '%' or '%%' when the query contains
        # only wildcard characters. Preserve the resulting match-all behavior.
        search_text = ""

    limit = max(1, min(int(limit), 40))

    sql = """
        SELECT
            scryfall_id,
            name,
            set_code,
            rarity,
            tcgplayer_id,
            collector_number
        FROM (
            SELECT
                e.external_id AS scryfall_id,
                COALESCE(c.name, p.metadata->>'name') AS name,
                s.set_code,
                p.rarity,
                m.target_external_product_id AS tcgplayer_id,
                p.collector_number,
                CASE
                    WHEN LEFT(
                        LOWER(COALESCE(c.name, p.metadata->>'name')),
                        CHAR_LENGTH(?)
                    ) = LOWER(?) THEN 0
                    WHEN LEFT(LOWER(s.set_code), CHAR_LENGTH(?)) = LOWER(?) THEN 1
                    WHEN LEFT(LOWER(p.collector_number), CHAR_LENGTH(?)) = LOWER(?) THEN 2
                    ELSE 3
                END AS match_rank
            FROM catalog.printings AS p
            JOIN catalog.external_ids AS e
              ON e.printing_id = p.printing_id
             AND e.game_id = p.game_id
             AND e.provider_id = 'scryfall'
             AND e.entity_type = 'printing'
             AND e.external_id_type = 'scryfall_id'
            LEFT JOIN catalog.cards AS c
              ON c.card_id = p.card_id
             AND c.game_id = p.game_id
            LEFT JOIN catalog.sets AS s
              ON s.set_id = p.set_id
             AND s.game_id = p.game_id
            LEFT JOIN catalog.printing_product_mappings AS m
              ON m.printing_id = p.printing_id
             AND m.game_id = p.game_id
             AND m.source_provider_id = 'scryfall'
             AND m.source_external_printing_id = e.external_id
             AND m.target_provider_id = 'tcgcsv'
            WHERE p.game_id = 'mtg'
              AND (
                  STRPOS(
                      LOWER(COALESCE(c.name, p.metadata->>'name')),
                      LOWER(?)
                  ) > 0
                  OR STRPOS(LOWER(s.set_code), LOWER(?)) > 0
                  OR STRPOS(LOWER(p.collector_number), LOWER(?)) > 0
              )
        ) AS catalog_matches
        ORDER BY
            match_rank,
            LOWER(name) COLLATE "C" ASC NULLS FIRST,
            LOWER(set_code) COLLATE "C" ASC NULLS FIRST,
            LOWER(collector_number) COLLATE "C" ASC NULLS FIRST
        LIMIT ?
    """

    params = (
        search_text, search_text,
        search_text, search_text,
        search_text, search_text,
        search_text, search_text, search_text,
        limit,
    )

    try:
        rows = conn.execute(sql, params).fetchall()
    except Exception as exc:
        raise RuntimeError(f"Card catalog search failed: {exc}") from exc

    return [
        {
            "scryfall_id": row["scryfall_id"],
            "name": row["name"],
            "set_code": row["set_code"],
            "rarity": row["rarity"],
            "tcgplayer_id": _normalize_tcgplayer_id(row["tcgplayer_id"]),
            "collector_number": row["collector_number"],
            "image_url": _scryfall_image_url(row["scryfall_id"]),
        }
        for row in rows
    ]
