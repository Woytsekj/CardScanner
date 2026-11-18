"""PostgreSQL read queries for Scryfall card information."""

from __future__ import annotations


def fetch_card_info(conn, scryfall_id: str) -> dict | None:
    """Return the legacy card-info response fields from the normalized catalog.

    The caller owns the connection. Artwork tags are collected across every
    printing face, while Oracle tags use the provider's external Oracle ID.
    """
    sid = str(scryfall_id or "").strip()

    row = conn.execute(
        """
        SELECT
            external_printing_id AS scryfall_id,
            name,
            type_line,
            colors_text,
            mana_value,
            mana_cost,
            oracle_text,
            external_oracle_id AS oracle_id,
            legalities,
            updated_at
        FROM catalog.provider_printing_metadata
        WHERE game_id = 'mtg'
          AND provider_id = 'scryfall'
          AND external_printing_id = ?
        """,
        (sid,),
    ).fetchone()

    if not row:
        return None

    art_rows = conn.execute(
        """
        SELECT tag_labels.label
        FROM (
            SELECT DISTINCT t.label AS label
            FROM catalog.external_ids AS e
            JOIN catalog.printing_faces AS pf
              ON pf.printing_id = e.printing_id
             AND pf.game_id = e.game_id
            JOIN catalog.art_tag_assignments AS ata
              ON ata.illustration_id = pf.illustration_id
             AND ata.game_id = pf.game_id
             AND ata.provider_id = e.provider_id
            JOIN catalog.tags AS t
              ON t.tag_id = ata.tag_id
             AND t.game_id = ata.game_id
             AND t.provider_id = ata.provider_id
            WHERE e.game_id = 'mtg'
              AND e.provider_id = 'scryfall'
              AND e.entity_type = 'printing'
              AND e.external_id_type = 'scryfall_id'
              AND e.external_id = ?
              AND t.tag_type = 'art'
        ) AS tag_labels
        ORDER BY lower(tag_labels.label), tag_labels.label
        LIMIT 100
        """,
        (sid,),
    ).fetchall()
    art_tags = [item["label"] for item in art_rows]

    oracle_tags: list[str] = []
    oracle_id = row["oracle_id"]

    if oracle_id:
        oracle_rows = conn.execute(
            """
            SELECT tag_labels.label
            FROM (
                SELECT DISTINCT t.label AS label
                FROM catalog.oracle_tag_assignments AS ota
                JOIN catalog.tags AS t
                  ON t.tag_id = ota.tag_id
                 AND t.game_id = ota.game_id
                 AND t.provider_id = ota.provider_id
                WHERE ota.game_id = 'mtg'
                  AND ota.provider_id = 'scryfall'
                  AND ota.external_oracle_id = ?
                  AND t.tag_type = 'oracle'
            ) AS tag_labels
            ORDER BY lower(tag_labels.label), tag_labels.label
            LIMIT 100
            """,
            (oracle_id,),
        ).fetchall()
        oracle_tags = [item["label"] for item in oracle_rows]

    legalities = row["legalities"]
    if not isinstance(legalities, dict):
        legalities = {}

    updated_at = row["updated_at"]
    if hasattr(updated_at, "isoformat"):
        updated_at = updated_at.isoformat()

    return {
        "scryfall_id": row["scryfall_id"],
        "name": row["name"],
        "type_line": row["type_line"],
        "colors": row["colors_text"] or "",
        "mana_value": row["mana_value"],
        "mana_cost": row["mana_cost"],
        "oracle_text": row["oracle_text"] or "",
        "oracle_id": oracle_id,
        "legalities": legalities,
        "art_tags": art_tags,
        "oracle_tags": oracle_tags,
        "updated_at": updated_at,
    }
