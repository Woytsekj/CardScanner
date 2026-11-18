"""PostgreSQL implementation of single-row inventory field updates."""

from __future__ import annotations

from postgres_app_db import connect_app_database


def update_inventory_field(
    *,
    item_id: int,
    field: str,
    value,
    normalize_finish,
) -> str:
    """Update one MTG inventory row and preserve legacy field semantics.

    Returns:
        updated
        not_found
        invalid_owner
        deck_not_found
        vending_conflict
    """
    columns = {
        "quantity": "quantity",
        "finish": "finish",
        "location": "location",
    }

    if field not in {"owner", "deck_id", "for_vending", *columns}:
        raise ValueError(f"Unsupported inventory field: {field}")

    conn = connect_app_database(expected_database="cardscanner_dev")

    try:
        # Lock referenced records before locking inventory. This keeps lock
        # ordering consistent with People rename/transfer and deck deletion.
        person_id = None
        if field == "owner":
            person = conn.execute(
                """
                SELECT id
                FROM app.people
                WHERE name = ? AND active = TRUE
                LIMIT 1
                FOR SHARE
                """,
                (value,),
            ).fetchone()

            if not person:
                conn.rollback()
                return "invalid_owner"

            person_id = int(person["id"])

        if field == "deck_id" and value is not None:
            deck = conn.execute(
                """
                SELECT id
                FROM app.decklists
                WHERE id = ? AND game_id = 'mtg'
                FOR KEY SHARE
                """,
                (value,),
            ).fetchone()

            if not deck:
                conn.rollback()
                return "deck_not_found"

        row = conn.execute(
            """
            SELECT id, deck_id
            FROM app.inventory
            WHERE id = ? AND game_id = 'mtg'
            FOR UPDATE
            """,
            (item_id,),
        ).fetchone()

        if not row:
            conn.rollback()
            return "not_found"

        if field == "owner":
            cursor = conn.execute(
                """
                UPDATE app.inventory
                SET owner = ?, person_id = ?
                WHERE id = ? AND game_id = 'mtg'
                """,
                (value, person_id, item_id),
            )

        elif field == "deck_id":
            if value is None:
                cursor = conn.execute(
                    """
                    UPDATE app.inventory
                    SET deck_id = NULL
                    WHERE id = ? AND game_id = 'mtg'
                    """,
                    (item_id,),
                )
            else:
                cursor = conn.execute(
                    """
                    UPDATE app.inventory
                    SET deck_id = ?, for_vending = FALSE
                    WHERE id = ? AND game_id = 'mtg'
                    """,
                    (value, item_id),
                )

        elif field == "for_vending":
            if bool(value) and row["deck_id"] is not None:
                conn.rollback()
                return "vending_conflict"

            cursor = conn.execute(
                """
                UPDATE app.inventory
                SET for_vending = ?
                WHERE id = ? AND game_id = 'mtg'
                """,
                (bool(value), item_id),
            )

        else:
            if field == "finish":
                value = normalize_finish(value)

            # Column names come exclusively from the fixed allowlist above.
            column = columns[field]
            cursor = conn.execute(
                f"""
                UPDATE app.inventory
                SET {column} = ?
                WHERE id = ? AND game_id = 'mtg'
                """,
                (value, item_id),
            )

        if cursor.rowcount != 1:
            conn.rollback()
            return "not_found"

        conn.commit()
        return "updated"

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
