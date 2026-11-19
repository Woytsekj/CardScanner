"""PostgreSQL persistence for admin-managed people and owner transfers."""

from __future__ import annotations


def _normalize_finish(value) -> str:
    value = str(value or "normal").strip().lower().replace("_", " ").replace("-", " ")
    return "foil" if value in {
        "foil", "etched", "foil etched", "etched foil", "traditional foil",
        "surge foil", "galaxy foil", "textured foil", "glossy foil",
    } else "normal"


def add_person(conn, name: str) -> str:
    """Insert a person, or reactivate an existing case-insensitive match."""
    inserted = conn.execute(
        """
        INSERT INTO app.people (name, active)
        VALUES (?, TRUE)
        ON CONFLICT DO NOTHING
        RETURNING id
        """,
        (name,),
    ).fetchone()
    if inserted:
        return "added"

    conn.execute(
        """
        UPDATE app.people
        SET active = TRUE
        WHERE lower(btrim(name)) = lower(btrim(?))
        """,
        (name,),
    )
    return "reactivated"


def rename_person(conn, person_id: int, name: str) -> str:
    """Rename a person and synchronize their MTG inventory owner labels."""
    person = conn.execute(
        "SELECT id, name FROM app.people WHERE id = ? FOR UPDATE",
        (person_id,),
    ).fetchone()
    if not person:
        return "not_found"

    duplicate = conn.execute(
        """
        SELECT id
        FROM app.people
        WHERE lower(btrim(name)) = lower(btrim(?))
          AND id <> ?
        LIMIT 1
        """,
        (name, person_id),
    ).fetchone()
    if duplicate:
        return "duplicate"

    old_name = str(person["name"])
    conn.execute(
        "UPDATE app.people SET name = ? WHERE id = ?",
        (name, person_id),
    )
    conn.execute(
        """
        UPDATE app.inventory
        SET owner = ?, person_id = ?
        WHERE game_id = 'mtg'
          AND (owner = ? OR person_id = ?)
        """,
        (name, person_id, old_name, person_id),
    )
    return "renamed"


def toggle_person(conn, person_id: int) -> bool:
    """Flip active state; return False when the person does not exist."""
    row = conn.execute(
        """
        UPDATE app.people
        SET active = NOT active
        WHERE id = ?
        RETURNING active
        """,
        (person_id,),
    ).fetchone()
    return bool(row)


def delete_person(conn, person_id: int, transfer_to: str = "") -> dict:
    """Delete a person, transferring linked MTG inventory when necessary.

    Inventory rows merge only when printing, finish, location, deck assignment,
    and vending flag match. The caller owns the transaction.
    """
    person = conn.execute(
        "SELECT id, name FROM app.people WHERE id = ? FOR UPDATE",
        (person_id,),
    ).fetchone()
    if not person:
        return {"status": "not_found"}

    source_name = str(person["name"])
    rows = conn.execute(
        """
        SELECT
            id, scryfall_id, finish, location, deck_id,
            for_vending, quantity
        FROM app.inventory
        WHERE game_id = 'mtg'
          AND (owner = ? OR person_id = ?)
        ORDER BY id
        FOR UPDATE
        """,
        (source_name, person_id),
    ).fetchall()
    total_quantity = sum(int(row["quantity"] or 0) for row in rows)

    target = None
    if total_quantity > 0:
        transfer_to = " ".join(str(transfer_to or "").strip().split())[:100]
        if not transfer_to or transfer_to.casefold() == source_name.casefold():
            return {
                "status": "needs_transfer",
                "name": source_name,
                "quantity": total_quantity,
            }

        target = conn.execute(
            """
            SELECT id, name
            FROM app.people
            WHERE name = ? AND active = TRUE
            LIMIT 1
            FOR UPDATE
            """,
            (transfer_to,),
        ).fetchone()
        if not target or int(target["id"]) == int(person_id):
            return {
                "status": "invalid_target",
                "name": source_name,
                "quantity": total_quantity,
            }

        target_id = int(target["id"])
        target_name = str(target["name"])

        for row in rows:
            finish = _normalize_finish(row["finish"])
            location = str(row["location"] or "Free")
            deck_id = row["deck_id"]
            scryfall_id = row["scryfall_id"]
            vending = bool(row["for_vending"])

            existing = conn.execute(
                """
                SELECT id, quantity
                FROM app.inventory
                WHERE game_id = 'mtg'
                  AND (owner = ? OR person_id = ?)
                  AND COALESCE(scryfall_id, '') = COALESCE(?, '')
                  AND COALESCE(NULLIF(finish, ''), 'normal') = ?
                  AND COALESCE(location, 'Free') = ?
                  AND deck_id IS NOT DISTINCT FROM ?
                  AND COALESCE(for_vending, FALSE) = ?
                ORDER BY id
                LIMIT 1
                FOR UPDATE
                """,
                (
                    target_name, target_id, scryfall_id,
                    finish, location, deck_id, vending,
                ),
            ).fetchone()

            if existing:
                conn.execute(
                    """
                    UPDATE app.inventory
                    SET quantity = quantity + ?, owner = ?, person_id = ?
                    WHERE id = ?
                    """,
                    (
                        int(row["quantity"] or 0),
                        target_name,
                        target_id,
                        existing["id"],
                    ),
                )
                conn.execute(
                    "DELETE FROM app.inventory WHERE id = ?",
                    (row["id"],),
                )
            else:
                conn.execute(
                    """
                    UPDATE app.inventory
                    SET owner = ?, person_id = ?
                    WHERE id = ? AND game_id = 'mtg'
                    """,
                    (target_name, target_id, row["id"]),
                )

    # Decklist person_id is also protected by ON DELETE SET NULL.
    conn.execute(
        "UPDATE app.users SET person_id = NULL WHERE person_id = ?",
        (person_id,),
    )
    conn.execute("DELETE FROM app.people WHERE id = ?", (person_id,))

    return {
        "status": "deleted",
        "name": source_name,
        "quantity": total_quantity,
        "target_name": str(target["name"]) if target else None,
    }
