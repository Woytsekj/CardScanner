"""PostgreSQL deck settings, allocation, movement, and lifecycle operations."""

from __future__ import annotations

from psycopg.types.json import Jsonb


def _norm_name(value) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _finish(value) -> str:
    raw = str(value or "normal").strip().lower().replace("_", " ").replace("-", " ")
    return "foil" if raw in {
        "foil", "etched", "foil etched", "etched foil", "traditional foil",
        "surge foil", "galaxy foil", "textured foil", "glossy foil",
    } else "normal"


def _inventory_matches(deck_card, inventory_card, *, fuzzy: bool = False) -> bool:
    deck_name = _norm_name(deck_card.get("name"))
    inventory_name = _norm_name(inventory_card.get("name"))
    if _finish(deck_card.get("finish")) != _finish(inventory_card.get("finish")):
        return False

    if bool(deck_card.get("any_printing")):
        return bool(deck_name and inventory_name and deck_name == inventory_name)

    if fuzzy:
        deck_oracle = str(deck_card.get("card_oracle_id") or deck_card.get("oracle_id") or "").strip()
        inventory_oracle = str(inventory_card.get("oracle_id") or "").strip()
        if deck_oracle and inventory_oracle:
            return deck_oracle == inventory_oracle
        return bool(deck_name and inventory_name and deck_name == inventory_name)

    deck_sid = str(deck_card.get("scryfall_id") or "").strip()
    inventory_sid = str(inventory_card.get("scryfall_id") or "").strip()
    if deck_sid and inventory_sid:
        return deck_sid == inventory_sid
    return bool(deck_name and inventory_name and deck_name == inventory_name)


def _deck_card_rows(conn, deck_id: int, *, lock: bool = False) -> list[dict]:
    suffix = "FOR UPDATE OF dc" if lock else ""
    rows = conn.execute(
        f"""
        SELECT
            dc.*,
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
        WHERE dc.deck_id = ?
          AND dc.game_id = 'mtg'
        ORDER BY dc.id
        {suffix}
        """,
        (deck_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _inventory_rows(conn, *, owner: str | None = None, lock: bool = False) -> list[dict]:
    owner_clause = "AND i.owner = ?" if owner is not None else ""
    params = (owner,) if owner is not None else ()
    lock_clause = "FOR UPDATE OF i" if lock else ""
    rows = conn.execute(
        f"""
        SELECT
            i.*,
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
          {owner_clause}
        ORDER BY i.id
        {lock_clause}
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def _catalog_ids(conn, scryfall_id: str | None):
    sid = str(scryfall_id or "").strip()
    if not sid:
        return None, None
    row = conn.execute(
        """
        SELECT p.card_id, p.printing_id
        FROM catalog.external_ids AS e
        JOIN catalog.printings AS p
          ON p.printing_id = e.printing_id
         AND p.game_id = e.game_id
        WHERE e.game_id = 'mtg'
          AND e.provider_id = 'scryfall'
          AND e.entity_type = 'printing'
          AND e.external_id_type = 'scryfall_id'
          AND e.external_id = ?
        ORDER BY p.printing_id
        LIMIT 1
        """,
        (sid,),
    ).fetchone()
    if not row:
        return None, None
    return row["card_id"], row["printing_id"]


def _find_merge_target(conn, source: dict, deck_id: int, owner: str):
    sid = str(source.get("scryfall_id") or "").strip()
    identity_sql = (
        "scryfall_id = ?" if sid
        else "LOWER(BTRIM(name)) = LOWER(BTRIM(?))"
    )
    identity_value = sid or source.get("name")
    return conn.execute(
        f"""
        SELECT id, quantity
        FROM app.inventory
        WHERE game_id = 'mtg'
          AND owner = ?
          AND deck_id = ?
          AND COALESCE(location, 'Free') = ?
          AND {identity_sql}
          AND COALESCE(set_code, '') = COALESCE(?, '')
          AND COALESCE(collector_number, '') = COALESCE(?, '')
          AND COALESCE(NULLIF(finish, ''), 'normal') = ?
          AND COALESCE(for_vending, FALSE) IS FALSE
        ORDER BY id
        LIMIT 1
        FOR UPDATE
        """,
        (
            owner,
            deck_id,
            str(source.get("location") or "Free"),
            identity_value,
            source.get("set_code"),
            source.get("collector_number"),
            _finish(source.get("finish")),
        ),
    ).fetchone()


def _insert_allocated_copy(conn, source: dict, owner: str, deck_id: int, quantity: int):
    conn.execute(
        """
        INSERT INTO app.inventory (
            game_id, card_id, printing_id, scryfall_id, name, set_code,
            rarity, price, scan_time, quantity, owner, person_id, finish,
            collector_number, tcgplayer_id, location, deck_id, for_vending
        )
        VALUES (
            'mtg', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, FALSE
        )
        """,
        (
            source.get("card_id"),
            source.get("printing_id"),
            source.get("scryfall_id"),
            source["name"],
            source.get("set_code"),
            source.get("rarity"),
            source.get("price"),
            source.get("scan_time"),
            quantity,
            owner,
            source.get("person_id"),
            _finish(source.get("finish")),
            source.get("collector_number"),
            source.get("tcgplayer_id"),
            source.get("location") or "Free",
            deck_id,
        ),
    )


def _allocate_quantity(conn, source: dict, owner: str, deck_id: int, quantity: int) -> None:
    source_id = int(source["id"])
    source_quantity = int(source.get("quantity") or 0)
    if quantity <= 0 or quantity > source_quantity:
        raise ValueError("Inventory quantity changed during deck allocation.")

    if quantity == source_quantity:
        updated = conn.execute(
            """
            UPDATE app.inventory
            SET deck_id = ?, for_vending = FALSE
            WHERE id = ? AND game_id = 'mtg'
            RETURNING id
            """,
            (deck_id, source_id),
        ).fetchone()
        if not updated:
            raise RuntimeError("Inventory row changed during deck allocation.")
        source["quantity"] = 0
        return

    updated = conn.execute(
        """
        UPDATE app.inventory
        SET quantity = quantity - ?
        WHERE id = ? AND game_id = 'mtg' AND quantity >= ?
        RETURNING quantity
        """,
        (quantity, source_id, quantity),
    ).fetchone()
    if not updated:
        raise RuntimeError("Inventory quantity changed during deck allocation.")
    source["quantity"] = int(updated["quantity"])

    target = _find_merge_target(conn, source, deck_id, owner)
    if target:
        conn.execute(
            "UPDATE app.inventory SET quantity = quantity + ? WHERE id = ?",
            (quantity, target["id"]),
        )
    else:
        _insert_allocated_copy(conn, source, owner, deck_id, quantity)


def update_deck_settings(
    conn,
    deck_id: int,
    *,
    name: str,
    tags: list[str],
    person_name: str,
) -> str:
    """Update deck name, tags, and its one active owner."""
    person = conn.execute(
        """
        SELECT id, name
        FROM app.people
        WHERE lower(btrim(name)) = lower(btrim(?))
          AND active IS TRUE
        LIMIT 1
        """,
        (person_name,),
    ).fetchone()
    if not person:
        return "invalid_owner"

    deck = conn.execute(
        """
        SELECT id
        FROM app.decklists
        WHERE id = ? AND game_id = 'mtg'
        FOR UPDATE
        """,
        (deck_id,),
    ).fetchone()
    if not deck:
        return "deck_not_found"

    conn.execute(
        """
        UPDATE app.decklists
        SET name = ?, tags_json = ?, person_id = ?, person_name = ?,
            updated_at = now()
        WHERE id = ? AND game_id = 'mtg'
        """,
        (name, Jsonb(tags), int(person["id"]), str(person["name"]), deck_id),
    )
    return "updated"


def assign_free_to_deck(conn, deck_id: int, *, fuzzy: bool = False) -> dict:
    """Allocate this owner's free matching inventory to requirements in order."""
    deck = conn.execute(
        """
        SELECT id, person_id, person_name, name
        FROM app.decklists
        WHERE id = ? AND game_id = 'mtg'
        FOR UPDATE
        """,
        (deck_id,),
    ).fetchone()
    if not deck:
        return {"status": "deck_not_found"}

    owner = str(deck["person_name"] or "").strip()
    if not owner and deck["person_id"] is not None:
        person = conn.execute(
            "SELECT name FROM app.people WHERE id = ? AND active IS TRUE",
            (deck["person_id"],),
        ).fetchone()
        owner = str(person["name"]) if person else ""
    if not owner:
        return {"status": "missing_owner"}

    cards = _deck_card_rows(conn, deck_id, lock=True)
    inventory = _inventory_rows(conn, owner=owner, lock=True)
    moved = 0
    requested = 0

    for card in cards:
        already = sum(
            max(0, int(row.get("quantity") or 0))
            for row in inventory
            if row.get("deck_id") is not None
            and int(row["deck_id"]) == deck_id
            and _inventory_matches(card, row, fuzzy=fuzzy)
        )
        need = max(0, int(card.get("quantity") or 0) - already)
        requested += need
        if not need:
            continue

        for source in inventory:
            if need <= 0:
                break
            if source.get("deck_id") is not None:
                continue
            available = max(0, int(source.get("quantity") or 0))
            if not available or not _inventory_matches(card, source, fuzzy=fuzzy):
                continue

            take = min(need, available)
            _allocate_quantity(conn, source, owner, deck_id, take)
            moved += take
            need -= take

        # Include newly split/merged allocations before considering the next
        # requirement. This prevents duplicate requirements from allocating
        # the same free quantity more than once.
        inventory = _inventory_rows(conn, owner=owner, lock=True)

    if moved:
        conn.execute(
            "UPDATE app.decklists SET updated_at = now() WHERE id = ?",
            (deck_id,),
        )
    return {
        "status": "assigned",
        "moved": moved,
        "requested": requested,
        "owner": owner,
    }


def move_inventory_to_deck(
    conn,
    deck_id: int,
    inventory_id: int,
    quantity: int,
    *,
    fuzzy: bool = False,
) -> dict:
    """Move a physical copy from the free pool/another deck into this deck."""
    if quantity < 1:
        return {"status": "invalid_quantity"}

    deck = conn.execute(
        """
        SELECT id, name, person_id, person_name
        FROM app.decklists
        WHERE id = ? AND game_id = 'mtg'
        FOR UPDATE
        """,
        (deck_id,),
    ).fetchone()
    source_row = conn.execute(
        """
        SELECT
            i.*,
            COALESCE(
                NULLIF(c.metadata->>'oracle_id', ''),
                NULLIF(p.metadata->>'oracle_id', '')
            ) AS oracle_id
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
        WHERE i.id = ? AND i.game_id = 'mtg'
        FOR UPDATE OF i
        """,
        (inventory_id,),
    ).fetchone()

    if not deck or not source_row:
        return {"status": "not_found"}

    source = dict(source_row)
    owner = str(deck["person_name"] or "").strip()
    if not owner and deck["person_id"] is not None:
        person = conn.execute(
            "SELECT name FROM app.people WHERE id = ? AND active IS TRUE",
            (deck["person_id"],),
        ).fetchone()
        owner = str(person["name"]) if person else ""
    source_owner = str(source.get("owner") or "").strip()
    if not owner or source_owner.casefold() != owner.casefold():
        return {"status": "wrong_owner"}

    previous_deck_id = source.get("deck_id")
    if previous_deck_id is not None and int(previous_deck_id) == deck_id:
        return {"status": "already_assigned"}

    source_quantity = max(0, int(source.get("quantity") or 0))
    if quantity > source_quantity:
        return {"status": "insufficient_quantity", "available": source_quantity}

    requirements = _deck_card_rows(conn, deck_id, lock=True)
    matches = [
        card for card in requirements
        if _inventory_matches(card, source, fuzzy=fuzzy)
    ]
    if not matches:
        return {"status": "no_matching_requirement"}

    target_requirement = next(
        (
            card for card in matches
            if not _inventory_matches(card, source, fuzzy=False)
        ),
        matches[0],
    )

    _allocate_quantity(conn, source, owner, deck_id, quantity)

    printing_updated = False
    selected_sid = str(source.get("scryfall_id") or "").strip()
    required_sid = str(target_requirement.get("scryfall_id") or "").strip()
    source_set = str(source.get("set_code") or "")
    required_set = str(target_requirement.get("set_code") or "")
    source_collector = str(source.get("collector_number") or "")
    required_collector = str(target_requirement.get("collector_number") or "")
    printing_changed = bool(
        (selected_sid and selected_sid != required_sid)
        or (source_set and source_set.casefold() != required_set.casefold())
        or (source_collector and source_collector != required_collector)
        or _finish(source.get("finish")) != _finish(target_requirement.get("finish"))
    )

    if (
        (fuzzy or bool(target_requirement.get("any_printing")))
        and printing_changed
        and (selected_sid or source_set or source_collector)
    ):
        card_id, printing_id = _catalog_ids(conn, selected_sid)
        conn.execute(
            """
            UPDATE app.deck_cards
            SET name = ?,
                scryfall_id = COALESCE(?, scryfall_id),
                set_code = COALESCE(?, set_code),
                collector_number = COALESCE(?, collector_number),
                finish = ?,
                tcgplayer_id = COALESCE(?, tcgplayer_id),
                card_id = ?,
                printing_id = ?
            WHERE id = ? AND deck_id = ? AND game_id = 'mtg'
            """,
            (
                source["name"],
                source.get("scryfall_id"),
                source.get("set_code"),
                source.get("collector_number"),
                _finish(source.get("finish")),
                source.get("tcgplayer_id"),
                card_id,
                printing_id,
                target_requirement["id"],
                deck_id,
            ),
        )
        printing_updated = True

    conn.execute(
        "UPDATE app.decklists SET updated_at = now() WHERE id = ?",
        (deck_id,),
    )

    previous_name = "the free pool"
    if previous_deck_id is not None:
        previous = conn.execute(
            """
            SELECT name FROM app.decklists
            WHERE id = ? AND game_id = 'mtg'
            """,
            (previous_deck_id,),
        ).fetchone()
        previous_name = str(previous["name"]) if previous else "another deck"

    return {
        "status": "moved",
        "deck_name": str(deck["name"]),
        "card_name": str(source["name"]),
        "quantity": quantity,
        "previous_deck_name": previous_name,
        "printing_updated": printing_updated,
        "any_printing": bool(target_requirement.get("any_printing")),
    }


def delete_deck(conn, deck_id: int) -> dict:
    """Delete a deck and release all allocated inventory without losing copies."""
    deck = conn.execute(
        """
        SELECT id, name, cover_filename
        FROM app.decklists
        WHERE id = ? AND game_id = 'mtg'
        FOR UPDATE
        """,
        (deck_id,),
    ).fetchone()
    if not deck:
        return {"status": "not_found"}

    rows = conn.execute(
        """
        SELECT COALESCE(SUM(quantity), 0) AS quantity
        FROM app.inventory
        WHERE game_id = 'mtg' AND deck_id = ?
        """,
        (deck_id,),
    ).fetchone()
    moved_quantity = int(rows["quantity"] or 0)

    conn.execute(
        """
        UPDATE app.inventory
        SET deck_id = NULL
        WHERE game_id = 'mtg' AND deck_id = ?
        """,
        (deck_id,),
    )
    conn.execute(
        "DELETE FROM app.deck_cards WHERE game_id = 'mtg' AND deck_id = ?",
        (deck_id,),
    )
    conn.execute(
        "DELETE FROM app.decklists WHERE game_id = 'mtg' AND id = ?",
        (deck_id,),
    )
    return {
        "status": "deleted",
        "name": str(deck["name"]),
        "moved_quantity": moved_quantity,
        "cover_filename": deck["cover_filename"],
    }
