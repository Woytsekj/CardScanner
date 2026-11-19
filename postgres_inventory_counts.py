"""PostgreSQL implementation of inventory Normal/Foil count updates."""

from __future__ import annotations

from postgres_app_db import connect_app_database


def set_inventory_finish_counts(
    *,
    scryfall_id: str,
    owner: str,
    normal_qty: int,
    foil_qty: int,
    normalize_finish,
) -> bool:
    """Set Normal/Foil quantities for one MTG printing and owner.

    Returns False when no matching inventory row exists. The caller supplies
    the application's finish normalizer so SQLite and PostgreSQL use identical
    finish classification.
    """
    lock_key = f"inventory-counts|mtg|{scryfall_id}|{owner}"
    conn = connect_app_database(expected_database="cardscanner_dev")

    try:
        # Serialize updates even when the requested finish row does not exist.
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(?, 0))",
            (lock_key,),
        )

        rows = conn.execute(
            """
            SELECT
                id,
                game_id,
                card_id,
                printing_id,
                scryfall_id,
                name,
                set_code,
                rarity,
                price,
                owner,
                person_id,
                finish,
                collector_number,
                tcgplayer_id,
                location,
                deck_id,
                for_vending
            FROM app.inventory
            WHERE game_id = 'mtg'
              AND scryfall_id = ?
              AND owner = ?
            ORDER BY id
            FOR UPDATE
            """,
            (scryfall_id, owner),
        ).fetchall()

        if not rows:
            conn.rollback()
            return False

        # Retain the source metadata before deleting or changing either finish.
        source = rows[0]

        for finish, quantity in (
            ("normal", normal_qty),
            ("foil", foil_qty),
        ):
            existing = next(
                (
                    row for row in rows
                    if normalize_finish(row["finish"]) == finish
                ),
                None,
            )

            if existing:
                if quantity == 0:
                    conn.execute(
                        "DELETE FROM app.inventory WHERE id = ?",
                        (existing["id"],),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE app.inventory
                        SET quantity = ?
                        WHERE id = ?
                        """,
                        (quantity, existing["id"]),
                    )

            elif quantity > 0:
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
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source["game_id"],
                        source["card_id"],
                        source["printing_id"],
                        source["scryfall_id"],
                        source["name"],
                        source["set_code"],
                        source["rarity"],
                        source["price"],
                        quantity,
                        source["owner"],
                        source["person_id"],
                        finish,
                        source["collector_number"],
                        source["tcgplayer_id"],
                        source["location"] or "Free",
                        source["deck_id"],
                        source["for_vending"],
                    ),
                )

        conn.commit()
        return True

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
