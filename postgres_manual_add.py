"""PostgreSQL implementation of the admin manual-add inventory operation."""

from __future__ import annotations


def add_manual_card(
    conn,
    *,
    name: str,
    owner: str,
    finish: str,
    quantity: int,
    scryfall_id: str,
    set_code: str,
    collector_number: str | None,
    rarity: str,
    tcgplayer_id: str | None,
    price: str,
    location: str,
) -> str:
    """Add a manual card or merge it into a matching non-vending inventory row.

    The caller owns the transaction and must commit or roll it back.
    Returns "invalid_owner", "added", or "merged".
    """
    person = conn.execute(
        """
        SELECT id
        FROM app.people
        WHERE name = ?
          AND active = TRUE
        LIMIT 1
        """,
        (owner,),
    ).fetchone()

    if not person:
        return "invalid_owner"

    person_id = person["id"]

    existing = None
    if scryfall_id:
        # Serialize adds for this exact legacy merge key. This also protects
        # the no-existing-row case, which SELECT FOR UPDATE cannot lock.
        lock_key = f"mtg|{scryfall_id}|{owner}|{finish}|{location}"
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(?, 0))",
            (lock_key,),
        )

        existing = conn.execute(
            """
            SELECT id, quantity
            FROM app.inventory
            WHERE game_id = 'mtg'
              AND scryfall_id = ?
              AND owner = ?
              AND COALESCE(NULLIF(finish, ''), 'normal') = ?
              AND COALESCE(location, 'Free') = ?
              AND deck_id IS NULL
              AND COALESCE(for_vending, FALSE) = FALSE
            ORDER BY id
            LIMIT 1
            FOR UPDATE
            """,
            (scryfall_id, owner, finish, location),
        ).fetchone()

    if existing:
        conn.execute(
            """
            UPDATE app.inventory
            SET quantity = ?,
                price = ?,
                set_code = COALESCE(NULLIF(?, ''), set_code),
                rarity = COALESCE(NULLIF(?, ''), rarity),
                collector_number = COALESCE(?, collector_number),
                tcgplayer_id = COALESCE(?, tcgplayer_id),
                scan_time = now()
            WHERE id = ?
              AND game_id = 'mtg'
            """,
            (
                int(existing["quantity"] or 0) + quantity,
                price,
                set_code,
                rarity,
                collector_number,
                tcgplayer_id,
                existing["id"],
            ),
        )
        return "merged"

    card_id = None
    printing_id = None

    if scryfall_id:
        catalog_printing = conn.execute(
            """
            SELECT
                p.card_id,
                e.printing_id
            FROM catalog.external_ids AS e
            JOIN catalog.printings AS p
              ON p.printing_id = e.printing_id
             AND p.game_id = e.game_id
            WHERE e.game_id = 'mtg'
              AND e.provider_id = 'scryfall'
              AND e.entity_type = 'printing'
              AND e.external_id_type = 'scryfall_id'
              AND e.external_id = ?
            LIMIT 1
            """,
            (scryfall_id,),
        ).fetchone()

        if catalog_printing:
            card_id = catalog_printing["card_id"]
            printing_id = catalog_printing["printing_id"]

    conn.execute(
        """
        INSERT INTO app.inventory (
            game_id,
            card_id,
            printing_id,
            scryfall_id,
            name,
            set_code,
            rarity,
            price,
            scan_time,
            quantity,
            owner,
            person_id,
            finish,
            collector_number,
            tcgplayer_id,
            location,
            deck_id,
            for_vending
        )
        VALUES (
            'mtg', ?, ?, ?, ?, ?, ?, ?, now(), ?, ?, ?, ?, ?, ?, ?, NULL, FALSE
        )
        """,
        (
            card_id,
            printing_id,
            scryfall_id or None,
            name,
            set_code,
            rarity,
            price,
            quantity,
            owner,
            person_id,
            finish,
            collector_number,
            tcgplayer_id,
            location,
        ),
    )

    return "added"
