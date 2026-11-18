"""Read-only PostgreSQL queries for inventory autocomplete."""

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


def fetch_inventory_search_suggestions(
    conn,
    query: str,
    owner: str | None = None,
    limit: int = 12,
) -> list[dict]:
    """Search owned inventory by name, alias, collector number, or set.

    Quantities are aggregated by printing/name/set/collector number, matching
    the legacy SQLite endpoint. An optional owner filter is applied before
    aggregation. The caller owns the connection.
    """
    search = str(query or "").strip()[:80]
    if len(search) < 2:
        return []

    # Match the legacy _like_pattern/_prefix_pattern behavior: '%' and '_'
    # supplied by a user are removed rather than treated as SQL wildcards.
    search = search.replace("%", "").replace("_", "")

    owner_filter = str(owner or "").strip()
    limit = max(1, min(int(limit), 12))

    sql = """
        SELECT
            i.scryfall_id,
            i.name,
            i.set_code,
            COALESCE(i.collector_number, p.collector_number) AS collector_number,
            SUM(COALESCE(i.quantity, 1)) AS total_quantity,

            COALESCE((
                SELECT STRING_AGG(ca.alias, ' · ' ORDER BY ca.id)
                FROM app.card_aliases AS ca
                WHERE ca.game_id = 'mtg'
                  AND ca.provider_id = 'scryfall'
                  AND ca.external_printing_id = i.scryfall_id
            ), '') AS alternate_names,

            (
                SELECT ca.alias
                FROM app.card_aliases AS ca
                WHERE ca.game_id = 'mtg'
                  AND ca.provider_id = 'scryfall'
                  AND ca.external_printing_id = i.scryfall_id
                  AND STRPOS(LOWER(ca.alias), LOWER(?)) > 0
                ORDER BY
                    CASE
                        WHEN STRPOS(LOWER(ca.alias), LOWER(?)) = 1 THEN 0
                        ELSE 1
                    END,
                    LOWER(ca.alias) COLLATE "C",
                    ca.alias COLLATE "C"
                LIMIT 1
            ) AS matched_alias,

            CASE
                WHEN STRPOS(LOWER(i.name), LOWER(?)) = 1 THEN 0
                WHEN EXISTS (
                    SELECT 1
                    FROM app.card_aliases AS ca2
                    WHERE ca2.game_id = 'mtg'
                      AND ca2.provider_id = 'scryfall'
                      AND ca2.external_printing_id = i.scryfall_id
                      AND STRPOS(LOWER(ca2.alias), LOWER(?)) > 0
                ) THEN 1
                WHEN STRPOS(
                    LOWER(COALESCE(i.collector_number, p.collector_number)),
                    LOWER(?)
                ) > 0 THEN 2
                WHEN STRPOS(LOWER(COALESCE(i.set_code, '')), LOWER(?)) > 0 THEN 3
                ELSE 4
            END AS match_rank

        FROM app.inventory AS i

        LEFT JOIN catalog.external_ids AS e
          ON e.game_id = i.game_id
         AND e.provider_id = 'scryfall'
         AND e.entity_type = 'printing'
         AND e.external_id_type = 'scryfall_id'
         AND e.external_id = i.scryfall_id

        LEFT JOIN catalog.printings AS p
          ON p.game_id = e.game_id
         AND p.printing_id = e.printing_id

        WHERE i.game_id = 'mtg'
          AND (
              STRPOS(LOWER(i.name), LOWER(?)) > 0
              OR STRPOS(
                  LOWER(COALESCE(i.collector_number, p.collector_number)),
                  LOWER(?)
              ) > 0
              OR STRPOS(LOWER(COALESCE(i.set_code, '')), LOWER(?)) > 0
              OR EXISTS (
                  SELECT 1
                  FROM app.card_aliases AS ca3
                  WHERE ca3.game_id = 'mtg'
                    AND ca3.provider_id = 'scryfall'
                    AND ca3.external_printing_id = i.scryfall_id
                    AND STRPOS(LOWER(ca3.alias), LOWER(?)) > 0
              )
          )
          AND (
              ? = ''
              OR LOWER(COALESCE(i.owner, 'Me')) = LOWER(?)
          )

        GROUP BY
            i.scryfall_id,
            i.name,
            i.set_code,
            COALESCE(i.collector_number, p.collector_number)

        ORDER BY
            match_rank,
            LOWER(i.name) COLLATE "C" ASC,
            LOWER(i.set_code) COLLATE "C" ASC NULLS FIRST,
            LOWER(COALESCE(i.collector_number, p.collector_number))
                COLLATE "C" ASC NULLS FIRST

        LIMIT ?
    """

    params = (
        search,  # matched alias contains
        search,  # matched alias prefix priority
        search,  # name prefix rank
        search,  # alias rank
        search,  # collector number rank
        search,  # set code rank
        search,  # name search
        search,  # collector number search
        search,  # set code search
        search,  # alias search
        owner_filter,
        owner_filter,
        limit,
    )

    rows = conn.execute(sql, params).fetchall()

    return [
        {
            "scryfall_id": row["scryfall_id"],
            "name": row["name"],
            "set_code": row["set_code"],
            "collector_number": row["collector_number"],
            "quantity": int(row["total_quantity"] or 0),
            "alternate_names": row["alternate_names"] or "",
            "matched_alias": row["matched_alias"],
            "image_url": _scryfall_image_url(row["scryfall_id"]),
        }
        for row in rows
    ]
