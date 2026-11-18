"""PostgreSQL persistence for application-managed card aliases."""

from __future__ import annotations


def fetch_aliases(conn, scryfall_id: str) -> list[dict]:
    """Return aliases using the legacy route's field names and timestamp format."""
    sid = str(scryfall_id or "").strip()
    if not sid:
        return []

    rows = conn.execute(
        """
        SELECT
            id,
            alias,
            to_char(
                created_at AT TIME ZONE 'UTC',
                'YYYY-MM-DD HH24:MI:SS'
            ) AS created_at
        FROM app.card_aliases
        WHERE game_id = 'mtg'
          AND provider_id = 'scryfall'
          AND external_printing_id = ?
        ORDER BY lower(alias) COLLATE "C", alias COLLATE "C"
        """,
        (sid,),
    ).fetchall()

    return [
        {
            "id": int(row["id"]),
            "alias": row["alias"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def add_alias(conn, scryfall_id: str, alias: str) -> bool:
    """Add an alias only when the Scryfall printing exists in inventory.

    The external printing ID is retained when catalog resolution is absent.
    Returns False when there is no matching inventory printing.
    """
    sid = str(scryfall_id or "").strip()[:80]
    name = " ".join(str(alias or "").strip().split())[:200]
    if not sid or not name:
        return False

    exists = conn.execute(
        """
        SELECT 1
        FROM app.inventory
        WHERE game_id = 'mtg'
          AND scryfall_id = ?
        LIMIT 1
        """,
        (sid,),
    ).fetchone()
    if not exists:
        return False

    conn.execute(
        """
        INSERT INTO app.card_aliases (
            game_id,
            provider_id,
            external_printing_id,
            printing_id,
            alias
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
            ?
        )
        ON CONFLICT (
            game_id,
            provider_id,
            external_printing_id,
            (lower(alias))
        ) DO NOTHING
        """,
        (sid, sid, name),
    )
    return True


def delete_alias(conn, alias_id: int) -> bool:
    """Delete only an MTG/Scryfall alias; report whether a row was removed."""
    cursor = conn.execute(
        """
        DELETE FROM app.card_aliases
        WHERE id = ?
          AND game_id = 'mtg'
          AND provider_id = 'scryfall'
        """,
        (int(alias_id),),
    )
    return cursor.rowcount > 0
