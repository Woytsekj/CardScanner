"""PostgreSQL authentication queries for the staged CardScanner migration."""
from __future__ import annotations

from datetime import datetime

from postgres_app_db import connect_app_database


def load_current_user(username: str):
    """Load the session user and linked person without running SQLite bootstrap."""
    conn = connect_app_database()
    try:
        return conn.execute(
            """
            SELECT u.username, u.password_hash, u.is_admin, u.role,
                   u.is_superadmin, u.active, u.created_at,
                   u.last_login_at, u.person_id, u.manager_username,
                   p.name AS person_name, p.is_public AS collection_is_public
            FROM app.users AS u
            LEFT JOIN app.people AS p ON p.id = u.person_id
            WHERE lower(u.username) = lower(?)
            LIMIT 1
            """,
            (username,),
        ).fetchone()
    finally:
        conn.close()


def get_login_user(username: str):
    """Find a login account case-insensitively."""
    conn = connect_app_database()
    try:
        return conn.execute(
            """
            SELECT username, password_hash, is_admin, role,
                   is_superadmin, active
            FROM app.users
            WHERE lower(username) = lower(?)
            LIMIT 1
            """,
            (username,),
        ).fetchone()
    finally:
        conn.close()


def update_last_login(username: str, timestamp: datetime) -> None:
    """Record the successful login time using a timezone-aware timestamp."""
    conn = connect_app_database()
    try:
        conn.execute(
            """
            UPDATE app.users
            SET last_login_at = ?
            WHERE lower(username) = lower(?)
            """,
            (timestamp, username),
        )
        conn.commit()
    finally:
        conn.close()


def check_login_rate_limits(keys: list[str], now: float) -> bool:
    """Return False when any supplied rate-limit key is currently blocked."""
    conn = connect_app_database()
    try:
        for key in keys:
            row = conn.execute(
                "SELECT blocked_until FROM app.login_attempts WHERE key = ?",
                (key,),
            ).fetchone()

            if row and float(row["blocked_until"] or 0) > now:
                return False

        return True
    finally:
        conn.close()


def record_failed_login(
    keys: list[str],
    now: float,
    window_seconds: float,
    max_attempts: int,
    lockout_seconds: float,
) -> None:
    """Atomically increment failed attempts for each key."""
    conn = connect_app_database()
    try:
        for key in keys:
            # Insert a placeholder if this key has never been seen. The
            # following SELECT FOR UPDATE serializes concurrent updates.
            conn.execute(
                """
                INSERT INTO app.login_attempts
                    (key, first_attempt, attempts, blocked_until)
                VALUES (?, ?, 0, 0)
                ON CONFLICT (key) DO NOTHING
                """,
                (key, now),
            )

            row = conn.execute(
                """
                SELECT first_attempt, attempts
                FROM app.login_attempts
                WHERE key = ?
                FOR UPDATE
                """,
                (key,),
            ).fetchone()

            if row is None:
                raise RuntimeError("Could not lock login rate-limit record")

            first_attempt = float(row["first_attempt"])
            attempts = int(row["attempts"])

            if now - first_attempt > window_seconds:
                first_attempt = now
                attempts = 1
            else:
                attempts += 1

            blocked_until = (
                now + lockout_seconds if attempts >= max_attempts else 0.0
            )

            conn.execute(
                """
                UPDATE app.login_attempts
                SET first_attempt = ?, attempts = ?, blocked_until = ?
                WHERE key = ?
                """,
                (first_attempt, attempts, blocked_until, key),
            )

        conn.execute(
            "DELETE FROM app.login_attempts WHERE first_attempt < ?",
            (now - 86400,),
        )
        conn.commit()
    finally:
        conn.close()


def clear_login_failures(keys: list[str]) -> None:
    """Clear the supplied IP and username rate-limit records."""
    conn = connect_app_database()
    try:
        for key in keys:
            conn.execute(
                "DELETE FROM app.login_attempts WHERE key = ?",
                (key,),
            )
        conn.commit()
    finally:
        conn.close()


def load_account_person_ids(username: str) -> list[int]:
    """Return the single collection linked to a login, if one is assigned."""
    conn = connect_app_database()
    try:
        row = conn.execute(
            "SELECT person_id FROM app.users WHERE lower(username)=lower(?)",
            (username,),
        ).fetchone()
        return [int(row["person_id"])] if row and row["person_id"] is not None else []
    finally:
        conn.close()
