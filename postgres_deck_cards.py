"""PostgreSQL-backed add, edit, and delete operations for MTG deck cards."""

from __future__ import annotations

from uuid import UUID

from psycopg.types.json import Jsonb


def _valid_scryfall_id(value: str | None) -> str | None:
    if value is None:
        return None

    value = str(value).strip()
    if not value:
        return None

    try:
        UUID(value)
    except (ValueError, TypeError, AttributeError):
        return None

    return value


def _catalog_printing_by_scryfall_id(conn, scryfall_id: str):
    return conn.execute(
        """
        SELECT
            e.external_id AS scryfall_id,
            COALESCE(c.name, p.metadata->>'name') AS name,
            s.set_code,
            p.collector_number,
            p.card_id,
            p.printing_id,
            (
                SELECT m.target_external_product_id
                FROM catalog.printing_product_mappings AS m
                WHERE m.game_id = p.game_id
                  AND m.printing_id = p.printing_id
                  AND m.source_provider_id = 'scryfall'
                  AND m.source_external_printing_id = e.external_id
                  AND m.target_provider_id = 'tcgcsv'
                ORDER BY m.target_external_product_id
                LIMIT 1
            ) AS tcgplayer_id
        FROM catalog.external_ids AS e
        JOIN catalog.printings AS p
          ON p.printing_id = e.printing_id
         AND p.game_id = e.game_id
        LEFT JOIN catalog.cards AS c
          ON c.card_id = p.card_id
         AND c.game_id = p.game_id
        LEFT JOIN catalog.sets AS s
          ON s.set_id = p.set_id
         AND s.game_id = p.game_id
        WHERE e.game_id = 'mtg'
          AND e.provider_id = 'scryfall'
          AND e.entity_type = 'printing'
          AND e.external_id_type = 'scryfall_id'
          AND e.external_id = ?
        LIMIT 1
        """,
        (scryfall_id,),
    ).fetchone()


def _catalog_printings_by_name(
    conn,
    *,
    name: str,
    set_code: str | None,
    collector_number: str | None,
):
    clauses = [
        "p.game_id = 'mtg'",
        "e.game_id = p.game_id",
        "e.provider_id = 'scryfall'",
        "e.entity_type = 'printing'",
        "e.external_id_type = 'scryfall_id'",
        "LOWER(BTRIM(COALESCE(c.name, p.metadata->>'name'))) = LOWER(BTRIM(?))",
    ]
    params: list[object] = [name]

    if set_code:
        clauses.append("LOWER(BTRIM(s.set_code)) = LOWER(BTRIM(?))")
        params.append(set_code)

    if collector_number:
        clauses.append("p.collector_number = ?")
        params.append(collector_number)

    sql = f"""
        SELECT
            e.external_id AS scryfall_id,
            COALESCE(c.name, p.metadata->>'name') AS name,
            s.set_code,
            p.collector_number,
            p.card_id,
            p.printing_id,
            (
                SELECT m.target_external_product_id
                FROM catalog.printing_product_mappings AS m
                WHERE m.game_id = p.game_id
                  AND m.printing_id = p.printing_id
                  AND m.source_provider_id = 'scryfall'
                  AND m.source_external_printing_id = e.external_id
                  AND m.target_provider_id = 'tcgcsv'
                ORDER BY m.target_external_product_id
                LIMIT 1
            ) AS tcgplayer_id
        FROM catalog.printings AS p
        JOIN catalog.external_ids AS e
          ON e.printing_id = p.printing_id
         AND e.game_id = p.game_id
        LEFT JOIN catalog.cards AS c
          ON c.card_id = p.card_id
         AND c.game_id = p.game_id
        LEFT JOIN catalog.sets AS s
          ON s.set_id = p.set_id
         AND s.game_id = p.game_id
        WHERE {" AND ".join(clauses)}
        ORDER BY
            LOWER(s.set_code) NULLS FIRST,
            p.collector_number NULLS FIRST,
            p.printing_id
        LIMIT 12
    """
    return conn.execute(sql, params).fetchall()


def _resolve_card_fields(conn, values: dict) -> dict:
    """Resolve a unique exact printing while preserving unresolved identifiers."""
    name = values["name"]
    raw_id = str(values.get("scryfall_id") or "").strip() or None
    valid_id = _valid_scryfall_id(raw_id)
    set_code = values.get("set_code")
    collector = values.get("collector_number")

    resolved = None

    # An exact Scryfall ID is authoritative unless printing fields were
    # supplied to intentionally select a different printing.
    if valid_id and not (set_code or collector):
        resolved = _catalog_printing_by_scryfall_id(conn, valid_id)

    # Match the legacy resolver fallback: when an ID was supplied but could
    # not be resolved, try a unique name/set/collector match. Do not perform
    # name-only resolution when the request supplied no printing identifier.
    if resolved is None and (raw_id or set_code or collector):
        candidates = _catalog_printings_by_name(
            conn,
            name=name,
            set_code=set_code,
            collector_number=collector,
        )
        if len(candidates) == 1:
            resolved = candidates[0]

    if resolved is not None:
        return {
            "name": resolved["name"] or name,
            "scryfall_id": resolved["scryfall_id"],
            "set_code": resolved["set_code"] or set_code,
            "collector_number": resolved["collector_number"] or collector,
            "tcgplayer_id": resolved["tcgplayer_id"] or values.get("tcgplayer_id"),
            "card_id": resolved["card_id"],
            "printing_id": resolved["printing_id"],
        }

    # Keep unresolved legacy/import identifiers as text. They are not
    # canonical catalog IDs, so their normalized references remain NULL.
    return {
        "name": name,
        "scryfall_id": raw_id,
        "set_code": set_code,
        "collector_number": collector,
        "tcgplayer_id": values.get("tcgplayer_id"),
        "card_id": None,
        "printing_id": None,
    }


def _is_basic_land_name(value: str) -> bool:
    normalized = " ".join(str(value or "").casefold().split())
    return normalized in {
        "plains",
        "island",
        "swamp",
        "mountain",
        "forest",
        "wastes",
        "snow-covered plains",
        "snow-covered island",
        "snow-covered swamp",
        "snow-covered mountain",
        "snow-covered forest",
    }


def _lock_mtg_deck(conn, deck_id: int) -> bool:
    row = conn.execute(
        """
        SELECT id
        FROM app.decklists
        WHERE id = ?
          AND game_id = 'mtg'
        FOR UPDATE
        """,
        (deck_id,),
    ).fetchone()
    return row is not None


def add_deck_card(conn, deck_id: int, values: dict) -> dict:
    """Insert one MTG deck requirement. Caller owns commit/rollback."""
    if not _lock_mtg_deck(conn, deck_id):
        return {"status": "deck_not_found"}

    resolved = _resolve_card_fields(conn, values)
    any_printing = _is_basic_land_name(resolved["name"])

    row = conn.execute(
        """
        INSERT INTO app.deck_cards (
            deck_id, game_id, name, quantity, scryfall_id,
            set_code, collector_number, category, finish,
            tcgplayer_id, role_tags_json, any_printing,
            card_id, printing_id
        )
        VALUES (?, 'mtg', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id
        """,
        (
            deck_id,
            resolved["name"],
            values["quantity"],
            resolved["scryfall_id"],
            resolved["set_code"],
            resolved["collector_number"],
            values["category"],
            values["finish"],
            resolved["tcgplayer_id"],
            Jsonb(values["role_tags"]),
            any_printing,
            resolved["card_id"],
            resolved["printing_id"],
        ),
    ).fetchone()

    conn.execute(
        """
        UPDATE app.decklists
        SET updated_at = now()
        WHERE id = ? AND game_id = 'mtg'
        """,
        (deck_id,),
    )

    return {
        "status": "added",
        "id": int(row["id"]),
        "name": resolved["name"],
    }


def edit_deck_card(
    conn,
    deck_id: int,
    card_id: int,
    values: dict,
) -> dict:
    """Update one MTG deck requirement. Caller owns commit/rollback."""
    if not _lock_mtg_deck(conn, deck_id):
        return {"status": "deck_not_found"}

    existing = conn.execute(
        """
        SELECT id
        FROM app.deck_cards
        WHERE id = ?
          AND deck_id = ?
          AND game_id = 'mtg'
        FOR UPDATE
        """,
        (card_id, deck_id),
    ).fetchone()

    if not existing:
        return {"status": "card_not_found"}

    resolved = _resolve_card_fields(conn, values)

    conn.execute(
        """
        UPDATE app.deck_cards
        SET name = ?,
            quantity = ?,
            scryfall_id = ?,
            set_code = ?,
            collector_number = ?,
            category = ?,
            finish = ?,
            tcgplayer_id = ?,
            role_tags_json = ?,
            any_printing = ?,
            card_id = ?,
            printing_id = ?
        WHERE id = ?
          AND deck_id = ?
          AND game_id = 'mtg'
        """,
        (
            resolved["name"],
            values["quantity"],
            resolved["scryfall_id"],
            resolved["set_code"],
            resolved["collector_number"],
            values["category"],
            values["finish"],
            resolved["tcgplayer_id"],
            Jsonb(values["role_tags"]),
            values["any_printing"],
            resolved["card_id"],
            resolved["printing_id"],
            card_id,
            deck_id,
        ),
    )

    conn.execute(
        """
        UPDATE app.decklists
        SET updated_at = now()
        WHERE id = ? AND game_id = 'mtg'
        """,
        (deck_id,),
    )

    return {"status": "updated", "name": resolved["name"]}


def delete_deck_card(conn, deck_id: int, card_id: int) -> dict:
    """Delete one MTG deck requirement. Caller owns commit/rollback."""
    if not _lock_mtg_deck(conn, deck_id):
        return {"status": "deck_not_found"}

    deleted = conn.execute(
        """
        DELETE FROM app.deck_cards
        WHERE id = ?
          AND deck_id = ?
          AND game_id = 'mtg'
        RETURNING id
        """,
        (card_id, deck_id),
    ).fetchone()

    if not deleted:
        return {"status": "card_not_found"}

    conn.execute(
        """
        UPDATE app.decklists
        SET updated_at = now()
        WHERE id = ? AND game_id = 'mtg'
        """,
        (deck_id,),
    )

    return {"status": "deleted"}
