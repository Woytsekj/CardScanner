"""Read-only PostgreSQL queries for the supported admin page."""

from __future__ import annotations


def fetch_admin_page_data(conn):
    """Return people and users in the shapes expected by admin.html."""
    people = conn.execute(
        """
        SELECT
            p.id,
            p.name,
            p.active,
            p.created_at,
            COALESCE(
                (
                    SELECT SUM(COALESCE(i.quantity, 1))
                    FROM app.inventory AS i
                    WHERE i.game_id = 'mtg'
                      AND COALESCE(i.owner, 'Me') = p.name
                ),
                0
            ) AS card_count
        FROM app.people AS p
        ORDER BY p.active DESC, lower(p.name) COLLATE "C"
        """
    ).fetchall()

    users = conn.execute(
        """
        SELECT
            u.username,
            u.is_admin,
            u.role,
            u.is_superadmin,
            u.active,
            u.created_at,
            u.last_login_at,
            u.person_id,
            p.name AS person_name
        FROM app.users AS u
        LEFT JOIN app.people AS p ON p.id = u.person_id
        ORDER BY u.is_superadmin DESC, lower(u.username) COLLATE "C"
        """
    ).fetchall()

    return people, users
