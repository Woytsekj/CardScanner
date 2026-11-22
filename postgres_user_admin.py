"""PostgreSQL-backed superadmin management of web accounts."""

from __future__ import annotations

from flask import redirect, request, session, url_for
from werkzeug.security import generate_password_hash

from postgres_app_db import connect_app_database


# Serialize changes to account roles, active-admin counts, usernames,
# and person assignments. Database constraints remain the final safeguard.
_ACCOUNT_ADMIN_LOCK = 731942810553


def _redirect(message: str):
    return redirect(url_for("admin_page", message=message))


def _run(operation, *, lock: bool = False):
    conn = connect_app_database(expected_database="cardscanner_dev")
    try:
        if lock:
            conn.execute(
                "SELECT pg_advisory_xact_lock(?)",
                (_ACCOUNT_ADMIN_LOCK,),
            )

        result = operation(conn)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _active_admin_count(conn) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM app.users
        WHERE (is_admin = TRUE OR is_superadmin = TRUE)
          AND active = TRUE
        """
    ).fetchone()
    return int(row[0])


def handle_add_user():
    username = " ".join(
        str(request.form.get("username") or "").strip().split()
    )[:150]
    password = str(request.form.get("password") or "")
    role = str(request.form.get("role") or "user").strip().lower()
    raw_person_id = str(request.form.get("person_id") or "").strip()
    actor_username = str(session.get("username") or "").strip()
    actor_superadmin = bool(session.get("is_superadmin"))
    requested_manager = str(request.form.get("manager_username") or "").strip()
    if not actor_superadmin:
        role = "user"
        manager_username = actor_username
    else:
        manager_username = requested_manager or (
            actor_username if role in {"admin", "user"} else None
        )
    if role not in {"admin", "user", "vendor"}:
        role = "user"

    if not username or len(username) < 2:
        return _redirect("Username must be at least 2 characters.")
    if len(password) < 12:
        return _redirect("User password must be at least 12 characters.")
    if role not in {"admin", "user", "vendor"}:
        role = "user"

    person_id = None
    if raw_person_id:
        try:
            person_id = int(raw_person_id)
        except ValueError:
            return _redirect("Invalid person assignment.")

    password_hash = generate_password_hash(password)

    def operation(conn):
        manager_name = manager_username
        existing = conn.execute(
            """
            SELECT username
            FROM app.users
            WHERE lower(btrim(username)) = lower(btrim(?))
            LIMIT 1
            """,
            (username,),
        ).fetchone()
        if existing:
            return "That username already exists."

        if person_id is not None:
            person = conn.execute(
                """
                SELECT id, name, active
                FROM app.people
                WHERE id = ?
                """,
                (person_id,),
            ).fetchone()
            if not person or not bool(person["active"]):
                return "Choose an active person for this account."
            assigned = conn.execute(
                "SELECT username FROM app.users WHERE person_id = ? LIMIT 1",
                (person_id,),
            ).fetchone()
            if assigned:
                return "That person is already assigned to another account."

        if manager_name:
            manager = conn.execute(
                "SELECT username, is_admin, is_superadmin, active FROM app.users WHERE lower(username)=lower(?)",
                (manager_name,),
            ).fetchone()
            if not manager or not bool(manager["active"]) or not (
                bool(manager["is_admin"]) or bool(manager["is_superadmin"])
            ):
                return "Choose an active administrator as the manager."
            if role == "admin" and not bool(manager["is_superadmin"]):
                return "Admin accounts must report directly to a Superadmin."
            manager_name = str(manager["username"])

        inserted = conn.execute(
            """
            INSERT INTO app.users (
                username, password_hash, is_admin, role,
                is_superadmin, active, person_id, manager_username
            )
            VALUES (?, ?, ?, ?, FALSE, TRUE, ?, ?)
            ON CONFLICT DO NOTHING
            RETURNING username
            """,
            (
                username,
                password_hash,
                role == "admin",
                role,
                person_id,
                manager_name,
            ),
        ).fetchone()
        if not inserted:
            duplicate = conn.execute(
                """
                SELECT username
                FROM app.users
                WHERE lower(btrim(username)) = lower(btrim(?))
                LIMIT 1
                """,
                (username,),
            ).fetchone()
            if duplicate:
                return "That username already exists."
            if person_id is not None:
                return "That person is already assigned to another account."
            return "Could not create the account because of a database conflict."

        linked = conn.execute(
            """
            SELECT p.name
            FROM app.users u
            JOIN app.people p ON p.id = u.person_id
            WHERE lower(u.username) = lower(?)
            """,
            (username,),
        ).fetchone()
        assignment_note = f" and assigned collection {linked['name']}" if linked else ""
        return f"Created {'Admin' if role == 'admin' else 'user'} {username}{assignment_note}."

    return _redirect(_run(operation, lock=True))


def handle_assign_user_person(username: str):
    username = str(username)[:150]
    raw_person_id = str(request.form.get("person_id") or "").strip()

    person_id = None
    if raw_person_id:
        try:
            person_id = int(raw_person_id)
        except ValueError:
            return _redirect("Invalid person assignment.")

    def operation(conn):
        user = conn.execute(
            """
            SELECT username
            FROM app.users
            WHERE lower(btrim(username)) = lower(btrim(?))
            LIMIT 1
            FOR UPDATE
            """,
            (username,),
        ).fetchone()
        if not user:
            return "Web user not found."

        actual_username = str(user["username"])
        person_name = None

        if person_id is not None:
            person = conn.execute(
                """
                SELECT id, name, active
                FROM app.people
                WHERE id = ?
                """,
                (person_id,),
            ).fetchone()
            if not person or not bool(person["active"]):
                return "Choose an active person."
            if not bool(session.get("is_superadmin")):
                actor_username = str(session.get("username") or "")
                manageable = conn.execute(
                    """
                    SELECT 1 FROM app.users
                    WHERE lower(username)=lower(?) AND person_id=?
                    UNION
                    SELECT 1 FROM app.users
                    WHERE lower(manager_username)=lower(?)
                      AND lower(role) IN ('user', 'member')
                      AND person_id=?
                    UNION
                    SELECT 1 FROM app.people p
                    WHERE p.id=? AND NOT EXISTS (
                        SELECT 1 FROM app.users u WHERE u.person_id=p.id
                    )
                    LIMIT 1
                    """,
                    (actor_username, person_id, actor_username, person_id, person_id),
                ).fetchone()
                if not manageable:
                    return "You can only assign your own, unassigned, or directly managed User collections."

            person_name = str(person["name"])
            other = conn.execute(
                """
                SELECT username
                FROM app.users
                WHERE person_id = ?
                  AND username <> ?
                LIMIT 1
                """,
                (person_id, actual_username),
            ).fetchone()
            if other:
                return (
                    f"{person_name} is already assigned to "
                    f"{other['username']}. Choose an unassigned collection instead."
                )

        conn.execute(
            "UPDATE app.users SET person_id = ? WHERE username = ?",
            (person_id, actual_username),
        )
        linked = conn.execute(
            """
            SELECT p.id, p.name
            FROM app.users u
            JOIN app.people p ON p.id = u.person_id
            WHERE u.username = ?
            """,
            (actual_username,),
        ).fetchone()
        if not linked:
            return f"Could not resolve a collection for {actual_username}."
        if person_id is None:
            return f"Assigned {actual_username} a new collection ({linked['name']})."
        return f"Linked {actual_username} to {linked['name']}."

    return _redirect(_run(operation, lock=True))


def handle_toggle_user(username: str):
    username = str(username)[:150]
    current_username = str(session.get("username") or "")

    if current_username and current_username.casefold() == username.casefold():
        return _redirect("You cannot deactivate your own account.")

    def operation(conn):
        user = conn.execute(
            """
            SELECT username, active, is_admin, is_superadmin, role
            FROM app.users
            WHERE lower(btrim(username)) = lower(btrim(?))
            LIMIT 1
            FOR UPDATE
            """,
            (username,),
        ).fetchone()

        if not user:
            return "User not found."

        actual_username = str(user["username"])
        was_active = bool(user["active"])

        if (
            (bool(user["is_admin"]) or bool(user["is_superadmin"]))
            and was_active
            and _active_admin_count(conn) <= 1
        ):
            return "At least one active administrator must remain."

        conn.execute(
            """
            UPDATE app.users
            SET active = NOT active
            WHERE username = ?
            """,
            (actual_username,),
        )
        return (
            f"{'Deactivated' if was_active else 'Activated'} "
            f"{actual_username}."
        )

    return _redirect(_run(operation, lock=True))


def handle_admin_reset_user_password(username: str):
    username = str(username)[:150]
    password = str(request.form.get("password") or "")

    if len(password) < 12:
        return _redirect("Password must be at least 12 characters.")

    password_hash = generate_password_hash(password)

    def operation(conn):
        changed = conn.execute(
            """
            UPDATE app.users
            SET password_hash = ?
            WHERE lower(btrim(username)) = lower(btrim(?))
            RETURNING username
            """,
            (password_hash, username),
        ).fetchone()
        return "Password reset." if changed else "User not found."

    return _redirect(_run(operation))


def handle_rename_user(username: str):
    old_username = str(username)[:150]
    new_username = " ".join(
        str(request.form.get("new_username") or "").strip().split()
    )[:150]

    if len(new_username) < 2:
        return _redirect("Username must be at least 2 characters.")

    current_username = str(session.get("username") or "")

    def operation(conn):
        user = conn.execute(
            """
            SELECT username
            FROM app.users
            WHERE lower(btrim(username)) = lower(btrim(?))
            LIMIT 1
            FOR UPDATE
            """,
            (old_username,),
        ).fetchone()
        if not user:
            return ("User not found.", False)

        actual_old = str(user["username"])
        existing = conn.execute(
            """
            SELECT username
            FROM app.users
            WHERE lower(btrim(username)) = lower(btrim(?))
              AND username <> ?
            LIMIT 1
            """,
            (new_username, actual_old),
        ).fetchone()
        if existing:
            return ("That username already exists.", False)

        conn.execute(
            "UPDATE app.users SET username = ? WHERE username = ?",
            (new_username, actual_old),
        )
        is_self = (
            bool(current_username)
            and current_username.casefold() == actual_old.casefold()
        )
        return (
            f"Renamed {actual_old} to {new_username}.",
            is_self,
        )

    message, is_self = _run(operation, lock=True)
    if is_self:
        session["username"] = new_username
    return _redirect(message)


def handle_change_user_role(username: str):
    username = str(username)[:150]
    role = str(request.form.get("role") or "user").strip().lower()
    if role not in {"admin", "user", "vendor"}:
        role = "vendor"
    if not bool(session.get("is_superadmin")):
        return _redirect("Only a superadmin can change account roles.")

    def operation(conn):
        user = conn.execute(
            """
            SELECT username, is_admin, is_superadmin, active
            FROM app.users
            WHERE lower(btrim(username)) = lower(btrim(?))
            LIMIT 1
            FOR UPDATE
            """,
            (username,),
        ).fetchone()

        if not user:
            return "User not found."
        if bool(user["is_superadmin"]):
            return "The superadmin account cannot be changed to a normal role."

        if bool(user["is_admin"]) and role == "vendor":
            if _active_admin_count(conn) <= 1:
                return "At least one active administrator must remain."

        actual_username = str(user["username"])
        conn.execute(
            """
            UPDATE app.users
            SET role = ?, is_admin = ?
            WHERE username = ?
            """,
            (role, role == "admin", actual_username),
        )
        return f"Updated {actual_username} to {role}."

    return _redirect(_run(operation, lock=True))


def handle_delete_user(username: str):
    username = str(username)[:150]
    current_username = str(session.get("username") or "")

    if current_username and current_username.casefold() == username.casefold():
        return _redirect("You cannot delete your own account.")

    def operation(conn):
        user = conn.execute(
            """
            SELECT username, is_admin, is_superadmin, active
            FROM app.users
            WHERE lower(btrim(username)) = lower(btrim(?))
            LIMIT 1
            FOR UPDATE
            """,
            (username,),
        ).fetchone()

        if not user:
            return "User not found."

        actual_username = str(user["username"])

        if bool(user["is_superadmin"]):
            active_superadmins = conn.execute(
                """
                SELECT COUNT(*)
                FROM app.users
                WHERE is_superadmin = TRUE AND active = TRUE
                """
            ).fetchone()[0]
            if int(active_superadmins) <= 1:
                return "You cannot delete the last superadmin account."

        if bool(user["is_admin"]) or bool(user["is_superadmin"]):
            if bool(user["active"]) and _active_admin_count(conn) <= 1:
                return "You cannot delete the last active administrator."

        conn.execute(
            "DELETE FROM app.users WHERE username = ?",
            (actual_username,),
        )
        return f"Deleted web user {actual_username} permanently."

    return _redirect(_run(operation, lock=True))

def handle_change_password():
    """Change the password of the currently authenticated account."""
    password = str(request.form.get("password") or "")
    if len(password) < 12:
        return redirect(url_for("preferences_page", message="Password must be at least 12 characters."))

    username = str(session.get("username") or "").strip()
    if not username:
        return redirect(url_for("preferences_page", message="User not found."))

    password_hash = generate_password_hash(password)

    def operation(conn):
        updated = conn.execute(
            """
            UPDATE app.users
            SET password_hash = ?
            WHERE lower(btrim(username)) = lower(btrim(?))
            RETURNING username
            """,
            (password_hash, username),
        ).fetchone()
        return updated is not None

    if not _run(operation):
        return redirect(url_for("preferences_page", message="User not found."))

    return redirect(url_for("preferences_page", message="Password updated."))

