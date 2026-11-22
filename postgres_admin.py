"""Read-only PostgreSQL queries for the supported admin page."""

from __future__ import annotations


def fetch_admin_page_data(conn, *, username=None, superadmin=False):
    """Return visible people and manageable users for the administration workspace."""
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
            u.manager_username,
            p.name AS person_name
        FROM app.users AS u
        LEFT JOIN app.people AS p ON p.id = u.person_id
        WHERE (? IS TRUE OR lower(u.manager_username) = lower(?))
        ORDER BY u.is_superadmin DESC, lower(u.username) COLLATE "C"
        """,
        (bool(superadmin), username),
    ).fetchall()

    users = [dict(account) for account in users]
    for account in users:
        rows = conn.execute(
            "SELECT person_id FROM app.user_people WHERE lower(username)=lower(?) ORDER BY person_id",
            (account["username"],),
        ).fetchall()
        account["person_ids"] = sorted({int(row["person_id"]) for row in rows} | (
            {int(account["person_id"])} if account["person_id"] is not None else set()
        ))

    return people, users
