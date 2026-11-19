"""PostgreSQL implementation of single-row MTG inventory deletion."""

from __future__ import annotations

from postgres_app_db import connect_app_database


def delete_inventory_item(*, item_id: int) -> bool:
    """Delete one MTG inventory row; return False when it does not exist.

    Scanner idempotency history is intentionally kept: app.scan_log.row_id
    is not a foreign key to inventory.
    """
    conn = connect_app_database(expected_database="cardscanner_dev")

    try:
        cursor = conn.execute(
            """
            DELETE FROM app.inventory
            WHERE id = ? AND game_id = 'mtg'
            """,
            (item_id,),
        )
        deleted = cursor.rowcount == 1
        conn.commit()
        return deleted

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()
