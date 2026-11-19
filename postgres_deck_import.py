"""PostgreSQL persistence for imported MTG decklists and optional inventory copies."""

from __future__ import annotations

from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from postgres_deck_cards import _is_basic_land_name, _resolve_card_fields


def _tcgplayer_id(value) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return str(int(float(raw)))
    except (TypeError, ValueError, OverflowError):
        return raw


def _price_text(value) -> str | None:
    if value in (None, ""):
        return None
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError, OverflowError):
        return None


def _tag_list(values) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for item in values or []:
        if isinstance(item, dict):
            item = item.get("name") or item.get("label") or item.get("tag") or item.get("value")
        if item is None:
            continue
        tag = " ".join(str(item).replace("\x00", "").split()).strip().lstrip("#").strip()
        if not tag:
            continue
        tag = tag[:48]
        key = tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        output.append(tag)
        if len(output) >= 30:
            break
    return output


def import_deck(
    conn,
    *,
    name: str,
    source: str,
    source_url: str | None,
    cards: list[dict],
    deck_tags: list[str],
    person_name: str,
    add_inventory: bool = False,
    inventory_owner: str = "Me",
    location: str = "Free",
    store_free: bool = False,
    price_snapshot: dict | None = None,
    normalize_finish,
) -> int:
    """Insert an imported deck and optional physical copies in the caller's transaction.

    Owner validation happens before any insert. The caller owns commit/rollback,
    so the deck requirements and optional inventory additions are atomic.
    """
    clean_name = " ".join(str(name or "").split())[:200]
    if not clean_name:
        raise ValueError("Deck name cannot be empty.")
    if not cards:
        raise ValueError("No cards were found in the decklist.")

    people_rows = conn.execute(
        """
        SELECT id, name
        FROM app.people
        WHERE active IS TRUE
        ORDER BY id
        """
    ).fetchall()
    people = {
        str(row["name"]).strip().casefold(): {
            "id": int(row["id"]),
            "name": str(row["name"]).strip(),
        }
        for row in people_rows
    }

    deck_person = people.get(str(person_name or "").strip().casefold())
    if deck_person is None:
        raise ValueError(f"Deck for person '{person_name}' is not an active person.")

    inventory_person = None
    if add_inventory:
        inventory_person = people.get(str(inventory_owner or "").strip().casefold())
        if inventory_person is None:
            raise ValueError(f"Owner '{inventory_owner}' is not active")

    clean_tags = _tag_list(deck_tags)
    now = datetime.now(timezone.utc)
    deck_row = conn.execute(
        """
        INSERT INTO app.decklists (
            game_id, name, source, source_url, created_at, updated_at,
            tags_json, person_id, person_name
        )
        VALUES ('mtg', ?, ?, ?, ?, ?, ?::jsonb, ?, ?)
        RETURNING id
        """,
        (
            clean_name,
            str(source or "manual")[:40],
            str(source_url).strip() if source_url else None,
            now,
            now,
            Jsonb(clean_tags),
            deck_person["id"],
            deck_person["name"],
        ),
    ).fetchone()
    deck_id = int(deck_row["id"])

    prepared_cards: list[dict] = []
    deck_rows = []
    for card in cards:
        card_name = " ".join(str(card.get("name") or "").split())
        if not card_name:
            continue
        try:
            quantity = max(1, int(card.get("quantity") or 1))
        except (TypeError, ValueError, OverflowError):
            quantity = 1

        resolved = _resolve_card_fields(
            conn,
            {
                **card,
                "name": card_name,
                "quantity": quantity,
                "role_tags": _tag_list(card.get("role_tags") or []),
            },
            allow_name_only=True,
        )
        finish = normalize_finish(card.get("finish"))
        role_tags = _tag_list(card.get("role_tags") or [])
        category = str(card.get("category") or "Mainboard").strip()[:100] or "Mainboard"
        any_printing = _is_basic_land_name(resolved["name"])

        prepared_cards.append({
            "resolved": resolved,
            "quantity": quantity,
            "finish": finish,
            "category": category,
            "role_tags": role_tags,
        })
        deck_rows.append(
            (
                deck_id,
                "mtg",
                resolved["name"],
                quantity,
                resolved.get("scryfall_id"),
                resolved.get("set_code"),
                resolved.get("collector_number"),
                category,
                finish,
                _tcgplayer_id(resolved.get("tcgplayer_id")),
                Jsonb(role_tags),
                any_printing,
                resolved.get("card_id"),
                resolved.get("printing_id"),
            )
        )

    if not deck_rows:
        raise ValueError("No usable cards were found in the decklist.")

    conn.executemany(
        """
        INSERT INTO app.deck_cards (
            deck_id, game_id, name, quantity, scryfall_id, set_code,
            collector_number, category, finish, tcgplayer_id,
            role_tags_json, any_printing, card_id, printing_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        deck_rows,
    )

    if add_inventory:
        target_deck_id = None if store_free else deck_id
        target_location = str(location or "Free").strip()[:120] or "Free"
        price_snapshot = price_snapshot if isinstance(price_snapshot, dict) else {}

        for item in prepared_cards:
            card = item["resolved"]
            quantity = item["quantity"]
            finish = item["finish"]
            tcg_id = _tcgplayer_id(card.get("tcgplayer_id"))
            prices = price_snapshot.get(tcg_id, {}) if tcg_id else {}
            current_price = _price_text(prices.get(finish)) if isinstance(prices, dict) else None
            scryfall_id = card.get("scryfall_id")
            existing = None

            if scryfall_id:
                existing = conn.execute(
                    """
                    SELECT id
                    FROM app.inventory
                    WHERE game_id = 'mtg'
                      AND scryfall_id = ?
                      AND lower(btrim(owner)) = lower(btrim(?))
                      AND COALESCE(NULLIF(finish, ''), 'normal') = ?
                      AND COALESCE(location, 'Free') = ?
                      AND deck_id IS NOT DISTINCT FROM ?
                      AND COALESCE(for_vending, FALSE) IS FALSE
                    ORDER BY id
                    LIMIT 1
                    FOR UPDATE
                    """,
                    (
                        scryfall_id,
                        inventory_person["name"],
                        finish,
                        target_location,
                        target_deck_id,
                    ),
                ).fetchone()

            if existing:
                conn.execute(
                    """
                    UPDATE app.inventory
                    SET quantity = quantity + ?,
                        scan_time = ?,
                        price = COALESCE(?, price),
                        collector_number = COALESCE(?, collector_number),
                        tcgplayer_id = COALESCE(?, tcgplayer_id),
                        card_id = COALESCE(?, card_id),
                        printing_id = COALESCE(?, printing_id),
                        person_id = COALESCE(person_id, ?),
                        deck_id = ?,
                        for_vending = FALSE
                    WHERE id = ?
                    """,
                    (
                        quantity,
                        now,
                        current_price,
                        card.get("collector_number"),
                        tcg_id,
                        card.get("card_id"),
                        card.get("printing_id"),
                        inventory_person["id"],
                        target_deck_id,
                        int(existing["id"]),
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO app.inventory (
                        game_id, card_id, printing_id, scryfall_id, name,
                        set_code, rarity, price, scan_time, quantity,
                        owner, person_id, finish, collector_number, tcgplayer_id,
                        location, deck_id, for_vending
                    )
                    VALUES (
                        'mtg', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, FALSE
                    )
                    """,
                    (
                        card.get("card_id"),
                        card.get("printing_id"),
                        scryfall_id,
                        card["name"],
                        card.get("set_code") or "",
                        card.get("rarity") or "Common",
                        current_price or "0.00",
                        now,
                        quantity,
                        inventory_person["name"],
                        inventory_person["id"],
                        finish,
                        card.get("collector_number"),
                        tcg_id,
                        target_location,
                        target_deck_id,
                    ),
                )

    return deck_id
