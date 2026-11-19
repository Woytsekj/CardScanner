"""PostgreSQL implementation of inventory quantity movement."""

from __future__ import annotations

from postgres_app_db import connect_app_database


def move_inventory_quantity(
    *,
    item_id: int,
    move_qty: int,
    destination: str,
    normalize_finish,
) -> tuple[str, tuple[int, str] | None]:
    """Move part or all of one MTG inventory row to another location.

    Returns:
        ("moved", None)
        ("same_location", None)
        ("not_found", None)
        ("quantity_exceeded", (available_quantity, source_location))

    A transaction-scoped advisory lock serializes quantity moves, matching
    the write-serialization provided by SQLite's BEGIN IMMEDIATE. Source and
    compatible destination rows are also locked explicitly.
    """
    conn = connect_app_database(expected_database="cardscanner_dev")

    try:
        conn.execute(
            """
            SELECT pg_advisory_xact_lock(
                hashtextextended('inventory-move-quantity|mtg', 0)
            )
            """
        )

        row = conn.execute(
            """
            SELECT *
            FROM app.inventory
            WHERE id = ? AND game_id = 'mtg'
            FOR UPDATE
            """,
            (item_id,),
        ).fetchone()

        if not row:
            conn.rollback()
            return "not_found", None

        source_qty = int(row["quantity"] or 0)
        source_location = str(row["location"] or "Free")

        if move_qty > source_qty:
            conn.rollback()
            return "quantity_exceeded", (source_qty, source_location)

        if destination.casefold() == source_location.casefold():
            conn.rollback()
            return "same_location", None

        finish = normalize_finish(row["finish"])
        target = None

        if row["scryfall_id"]:
            target = conn.execute(
                """
                SELECT id, quantity
                FROM app.inventory
                WHERE game_id = 'mtg'
                  AND scryfall_id = ?
                  AND owner = ?
                  AND COALESCE(NULLIF(finish, ''), 'normal') = ?
                  AND COALESCE(location, 'Free') = ?
                  AND COALESCE(deck_id, 0) = COALESCE(?, 0)
                  AND COALESCE(for_vending, FALSE) = COALESCE(?, FALSE)
                  AND id <> ?
                ORDER BY id
                LIMIT 1
                FOR UPDATE
                """,
                (
                    row["scryfall_id"],
                    row["owner"],
                    finish,
                    destination,
                    row["deck_id"],
                    row["for_vending"],
                    item_id,
                ),
            ).fetchone()
        else:
            target = conn.execute(
                """
                SELECT id, quantity
                FROM app.inventory
                WHERE game_id = 'mtg'
                  AND scryfall_id IS NULL
                  AND lower(name) = lower(?)
                  AND lower(set_code) = lower(?)
                  AND COALESCE(collector_number, '') = COALESCE(?, '')
                  AND owner = ?
                  AND COALESCE(NULLIF(finish, ''), 'normal') = ?
                  AND COALESCE(location, 'Free') = ?
                  AND COALESCE(deck_id, 0) = COALESCE(?, 0)
                  AND COALESCE(for_vending, FALSE) = COALESCE(?, FALSE)
                  AND id <> ?
                ORDER BY id
                LIMIT 1
                FOR UPDATE
                """,
                (
                    row["name"],
                    row["set_code"],
                    row["collector_number"],
                    row["owner"],
                    finish,
                    destination,
                    row["deck_id"],
                    row["for_vending"],
                    item_id,
                ),
            ).fetchone()

        if move_qty == source_qty:
            cursor = conn.execute(
                """
                UPDATE app.inventory
                SET location = ?
                WHERE id = ? AND game_id = 'mtg'
                """,
                (destination, item_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Inventory row changed during quantity movement.")

        else:
            cursor = conn.execute(
                """
                UPDATE app.inventory
                SET quantity = quantity - ?
                WHERE id = ? AND game_id = 'mtg'
                """,
                (move_qty, item_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Inventory row changed during quantity movement.")

            if target:
                cursor = conn.execute(
                    """
                    UPDATE app.inventory
                    SET quantity = quantity + ?
                    WHERE id = ? AND game_id = 'mtg'
                    """,
                    (move_qty, target["id"]),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("Destination inventory row disappeared.")
            else:
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
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        row["game_id"],
                        row["card_id"],
                        row["printing_id"],
                        row["scryfall_id"],
                        row["name"],
                        row["set_code"],
                        row["rarity"],
                        row["price"],
                        row["scan_time"],
                        move_qty,
                        row["owner"],
                        row["person_id"],
                        finish,
                        row["collector_number"],
                        row["tcgplayer_id"],
                        destination,
                        row["deck_id"],
                        row["for_vending"],
                    ),
                )

        conn.commit()
        return "moved", None

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()
