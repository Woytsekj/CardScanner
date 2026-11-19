"""PostgreSQL implementation of bulk MTG inventory updates."""

from __future__ import annotations

from postgres_app_db import connect_app_database


def _error(message: str, http_status: int = 400) -> dict:
    return {
        "status": "error",
        "error": message,
        "http_status": http_status,
    }


def apply_bulk_inventory_update(data) -> dict:
    """Apply the legacy bulk-edit operation to selected MTG inventory rows.

    The caller is responsible for authorization and CSRF validation.

    The operation is atomic. Owner text and normalized person_id are updated
    together. Deck assignment always clears vending eligibility. Unassigning
    a deck and enabling vending in the same request remains supported.

    Returns a dictionary with either:
        {"status": "success", "updated": int, "selected": int}
    or:
        {"status": "error", "error": str, "http_status": int}
    """
    if not isinstance(data, dict):
        return _error("Select one or more inventory rows.")

    raw_ids = data.get("ids")
    if not isinstance(raw_ids, list):
        return _error("Select one or more inventory rows.")

    ids: list[int] = []
    seen: set[int] = set()

    for raw_id in raw_ids:
        try:
            item_id = int(raw_id)
        except (TypeError, ValueError, OverflowError):
            continue

        # PostgreSQL BIGINT is signed 64-bit. Ignore values which cannot
        # represent an inventory identity rather than failing at bind time.
        if (
            item_id > 0
            and item_id <= 9_223_372_036_854_775_807
            and item_id not in seen
        ):
            seen.add(item_id)
            ids.append(item_id)

    if not ids:
        return _error("Select one or more inventory rows.")

    if len(ids) > 5000:
        return _error("Select at most 5,000 rows per bulk operation.")

    owner_supplied = (
        "owner" in data
        and data.get("owner") not in (None, "", "unchanged")
    )
    owner_value = (
        " ".join(str(data.get("owner") or "").split())[:100]
        if owner_supplied
        else None
    )

    vending_value = None
    raw_vending = data.get("for_vending", "unchanged")

    if raw_vending not in (None, "", "unchanged"):
        if raw_vending in (True, 1, "1", "true", "yes", "on"):
            vending_value = True
        elif raw_vending in (False, 0, "0", "false", "no", "off"):
            vending_value = False
        else:
            return _error(
                "Choose Mark for vending or Remove from vending."
            )

    location_supplied = (
        "location" in data
        and data.get("location") not in (None, "")
    )
    location_value = (
        str(data.get("location") or "").strip()[:120]
        if location_supplied
        else None
    )

    deck_supplied = (
        "deck_id" in data
        and data.get("deck_id") not in (None, "", "unchanged")
    )
    deck_value: int | None = None

    if deck_supplied:
        raw_deck = data.get("deck_id")
        if raw_deck not in ("none", "unassigned", "null"):
            try:
                deck_value = int(raw_deck)
            except (TypeError, ValueError, OverflowError):
                return _error("Invalid deck selection.")

    if not any((
        owner_supplied,
        vending_value is not None,
        location_supplied,
        deck_supplied,
    )):
        return _error("Choose at least one bulk change.")

    if deck_supplied and deck_value is not None and vending_value is True:
        return _error(
            "A card cannot be assigned to a deck and marked for vending "
            "in the same operation."
        )

    conn = connect_app_database(expected_database="cardscanner_dev")

    try:
        # Lock referenced records before inventory rows. This follows the
        # person/deck-before-inventory ordering used by the single-row route.
        person_id = None

        if owner_supplied:
            person = conn.execute(
                """
                SELECT id
                FROM app.people
                WHERE name = ? AND active = TRUE
                LIMIT 1
                FOR SHARE
                """,
                (owner_value,),
            ).fetchone()

            if not person:
                conn.rollback()
                return _error(
                    "Choose an active owner from the people list."
                )

            person_id = int(person["id"])

        if deck_supplied and deck_value is not None:
            deck = conn.execute(
                """
                SELECT id
                FROM app.decklists
                WHERE id = ? AND game_id = 'mtg'
                FOR KEY SHARE
                """,
                (deck_value,),
            ).fetchone()

            if not deck:
                conn.rollback()
                return _error("Selected deck does not exist.")

        placeholders = ", ".join("?" for _ in ids)

        # Lock the selected MTG rows in a stable order. Missing IDs and
        # IDs from other games are intentionally excluded from this set.
        selected_rows = conn.execute(
            f"""
            SELECT id, deck_id
            FROM app.inventory
            WHERE game_id = 'mtg'
              AND id IN ({placeholders})
            ORDER BY id
            FOR UPDATE
            """,
            ids,
        ).fetchall()

        assigned_count = sum(
            1 for row in selected_rows if row["deck_id"] is not None
        )

        if (
            vending_value is True
            and not (deck_supplied and deck_value is None)
            and assigned_count
        ):
            conn.rollback()
            return _error(
                f"{assigned_count} selected row(s) are assigned to decks. "
                "Unassign them before marking them for vending."
            )

        assignments: list[str] = []
        values: list[object] = []

        if owner_supplied:
            assignments.extend(("owner = ?", "person_id = ?"))
            values.extend((owner_value, person_id))

        if location_supplied:
            # An explicitly blank/whitespace-only destination means Free.
            assignments.append("location = ?")
            values.append(location_value or "Free")

        if deck_supplied:
            assignments.append("deck_id = ?")
            values.append(deck_value)

        if deck_supplied and deck_value is not None:
            # Assigning to a deck always removes sale eligibility.
            assignments.append("for_vending = FALSE")
        elif vending_value is not None:
            assignments.append("for_vending = ?")
            values.append(vending_value)

        cursor = conn.execute(
            f"""
            UPDATE app.inventory
            SET {", ".join(assignments)}
            WHERE game_id = 'mtg'
              AND id IN ({placeholders})
            """,
            values + ids,
        )

        updated = int(cursor.rowcount)
        conn.commit()

        return {
            "status": "success",
            "updated": updated,
            "selected": len(ids),
        }

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
