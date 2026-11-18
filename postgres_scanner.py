"""PostgreSQL persistence and lookup helpers for desktop/mobile scanner APIs."""

from __future__ import annotations

from datetime import datetime, timezone


def fetch_active_people(conn) -> list[str]:
    rows = conn.execute(
        """
        SELECT name
        FROM app.people
        WHERE active IS TRUE
        ORDER BY LOWER(name), name
        """
    ).fetchall()
    return [str(row["name"]) for row in rows]


def fetch_location_options(conn, owner: str | None = None) -> list[dict[str, str]]:
    if owner:
        rows = conn.execute(
            """
            SELECT name
            FROM (
                SELECT DISTINCT COALESCE(location, 'Free') AS name
                FROM app.inventory
                WHERE game_id = 'mtg' AND owner = ?
            ) AS locations
            ORDER BY LOWER(name), name
            """,
            (owner,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT name
            FROM (
                SELECT DISTINCT COALESCE(location, 'Free') AS name
                FROM app.inventory
                WHERE game_id = 'mtg'
            ) AS locations
            ORDER BY LOWER(name), name
            """
        ).fetchall()

    names = [str(row["name"] or "Free") for row in rows]
    if not any(name.casefold() == "free" for name in names):
        names.insert(0, "Free")
    return [{"id": name, "name": name} for name in names]


def _person_rows(conn, *, username: str | None = None, person_id=None):
    if username is None:
        return conn.execute(
            """
            SELECT p.id, p.name, u.username AS account_id
            FROM app.people AS p
            LEFT JOIN app.users AS u
              ON u.person_id = p.id
             AND u.active IS TRUE
            WHERE p.active IS TRUE
            ORDER BY LOWER(p.name), p.name
            """
        ).fetchall()

    return conn.execute(
        """
        SELECT p.id, p.name, u.username AS account_id
        FROM app.people AS p
        JOIN app.users AS u
          ON u.person_id = p.id
         AND u.active IS TRUE
        WHERE p.active IS TRUE
          AND u.username = ?
          AND p.id = ?
        """,
        (username, person_id),
    ).fetchall()


def _format_person_options(rows) -> list[dict]:
    return [
        {
            "id": int(row["id"]),
            "person_id": int(row["id"]),
            "name": str(row["name"]),
            "account_id": str(row["account_id"]) if row["account_id"] else None,
            "account_name": str(row["account_id"]) if row["account_id"] else None,
        }
        for row in rows
    ]


def fetch_scanner_options(conn) -> dict:
    people = _format_person_options(_person_rows(conn))
    accounts = conn.execute(
        """
        SELECT username AS id, username AS name
        FROM app.users
        WHERE active IS TRUE
        ORDER BY LOWER(username), username
        """
    ).fetchall()
    locations = fetch_location_options(conn)
    return {
        "people": people,
        "persons": people,
        "accounts": [dict(row) for row in accounts],
        "locations": locations,
        "storage_locations": locations,
    }


def fetch_mobile_authorized_people(conn, user, *, is_admin: bool) -> list[dict]:
    if is_admin:
        rows = _person_rows(conn)
    else:
        if user["person_id"] is None:
            return []
        rows = _person_rows(
            conn,
            username=str(user["username"]),
            person_id=user["person_id"],
        )
    return _format_person_options(rows)


def resolve_printing_price_mapping(conn, scryfall_id: str) -> dict:
    """Resolve a Scryfall printing to its current TCGCSV product mapping."""
    sid = str(scryfall_id or "").strip()
    empty = {
        "scryfall_id": sid,
        "tcgplayer_id": None,
        "finishes": [],
        "name": None,
        "set": None,
        "collector_number": None,
    }
    if not sid:
        return empty

    row = conn.execute(
        """
        SELECT
            e.external_id AS scryfall_id,
            COALESCE(
                NULLIF(m.target_external_product_id, ''),
                NULLIF(p.metadata->>'tcgplayer_id', '')
            ) AS tcgplayer_id,
            COALESCE(m.finishes, p.metadata->'finishes', '[]'::jsonb) AS finishes,
            COALESCE(c.name, p.metadata->>'name') AS name,
            s.set_code,
            p.collector_number
        FROM catalog.external_ids AS e
        JOIN catalog.printings AS p
          ON p.printing_id = e.printing_id
         AND p.game_id = e.game_id
        LEFT JOIN catalog.cards AS c
          ON c.card_id = p.card_id
         AND c.game_id = p.game_id
        LEFT JOIN catalog.sets AS s
          ON s.set_id = p.set_id
         AND s.game_id = p.game_id
        LEFT JOIN catalog.printing_product_mappings AS m
          ON m.game_id = e.game_id
         AND m.printing_id = p.printing_id
         AND m.source_provider_id = 'scryfall'
         AND m.source_external_printing_id = e.external_id
         AND m.target_provider_id = 'tcgcsv'
        WHERE e.game_id = 'mtg'
          AND e.provider_id = 'scryfall'
          AND e.entity_type = 'printing'
          AND e.external_id_type = 'scryfall_id'
          AND e.external_id = ?
        ORDER BY m.resolved_at DESC NULLS LAST
        LIMIT 1
        """,
        (sid,),
    ).fetchone()

    if not row:
        return empty

    finishes = row["finishes"]
    if not isinstance(finishes, list):
        finishes = []
    product_id = row["tcgplayer_id"]
    if product_id is not None:
        product_id = str(product_id).strip() or None

    return {
        "scryfall_id": sid,
        "tcgplayer_id": product_id,
        "finishes": finishes,
        "name": row["name"],
        "set": row["set_code"],
        "collector_number": row["collector_number"],
    }


def _scan_datetime(value):
    raw = str(value or "").strip()
    if not raw:
        return datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"Invalid scan_time {raw!r}; provide an ISO 8601 date or timestamp."
        ) from exc
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def insert_or_increment_scan(
    conn,
    payload: dict,
    *,
    parse_quantity,
    normalize_finish,
) -> dict:
    """Save a scanner observation with owner validation and retry idempotency."""
    scryfall_id = str(payload.get("scryfall_id") or "").strip()
    name = str(payload.get("name") or "").strip()
    if not scryfall_id or not name:
        raise ValueError("scryfall_id and name are required")

    submitted_owner = str(payload.get("owner") or "").strip()[:100]
    owner = submitted_owner or "Me"
    raw_person_id = payload.get("person_id")
    if raw_person_id not in (None, ""):
        try:
            person_id = int(raw_person_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("person_id must identify an active server person") from exc
        person = conn.execute(
            """
            SELECT p.id, p.name, u.username AS account_id
            FROM app.people AS p
            LEFT JOIN app.users AS u
              ON u.person_id = p.id AND u.active IS TRUE
            WHERE p.id = ? AND p.active IS TRUE
            LIMIT 1
            """,
            (person_id,),
        ).fetchone()
        if not person:
            raise ValueError("Selected person is not active on the server")
        canonical_owner = str(person["name"])
        if submitted_owner and submitted_owner.casefold() != canonical_owner.casefold():
            raise ValueError("Selected person ID and owner name do not match")
        owner = canonical_owner
        expected_account_id = str(person["account_id"] or "")
        supplied_account_id = str(payload.get("account_id") or "").strip()
        if supplied_account_id != expected_account_id:
            raise ValueError(
                "The selected person/account mapping is out of date. "
                "Refresh scanner options and try again."
            )

    person = conn.execute(
        """
        SELECT id
        FROM app.people
        WHERE LOWER(BTRIM(name)) = LOWER(BTRIM(?))
          AND active IS TRUE
        LIMIT 1
        """,
        (owner,),
    ).fetchone()
    if not person:
        raise ValueError(f"Owner '{owner}' is not an active person on the server")
    person_id = int(person["id"])

    quantity = parse_quantity(payload.get("quantity"))
    finish = normalize_finish(payload.get("finish"))
    price = str(payload.get("price") if payload.get("price") is not None else "0.00")
    set_code = str(payload.get("set_code") or "").strip().upper()
    rarity = str(payload.get("rarity") or "Common").strip() or "Common"
    collector_number = str(payload.get("collector_number") or "").strip() or None
    tcgplayer_id = str(payload.get("tcgplayer_id") or "").strip() or None
    location_raw = payload.get("location")
    if location_raw in (None, ""):
        location_raw = payload.get("storage_location")
    location = str(location_raw or "Free").strip()[:120] or "Free"
    raw_location_id = payload.get("location_id")
    if raw_location_id not in (None, "") and str(raw_location_id).strip().casefold() != location.casefold():
        raise ValueError(
            "Selected storage location ID and name do not match. "
            "Refresh scanner options and try again."
        )

    raw_vending = payload.get(
        "is_vending", payload.get("vending", payload.get("for_vending", False))
    )
    if isinstance(raw_vending, bool):
        for_vending = raw_vending
    elif raw_vending in (0, 1):
        for_vending = bool(raw_vending)
    elif str(raw_vending).strip().casefold() in {"true", "yes", "on", "1"}:
        for_vending = True
    elif str(raw_vending).strip().casefold() in {"false", "no", "off", "0", "", "none"}:
        for_vending = False
    else:
        raise ValueError("is_vending must be true or false")

    scan_time = _scan_datetime(payload.get("scan_time"))
    scan_id = str(payload.get("scan_id") or "").strip()[:64] or None
    if not tcgplayer_id:
        tcgplayer_id = resolve_printing_price_mapping(conn, scryfall_id).get("tcgplayer_id")

    printing = conn.execute(
        """
        SELECT p.card_id, p.printing_id
        FROM catalog.external_ids AS e
        JOIN catalog.printings AS p
          ON p.printing_id = e.printing_id AND p.game_id = e.game_id
        WHERE e.game_id = 'mtg'
          AND e.provider_id = 'scryfall'
          AND e.entity_type = 'printing'
          AND e.external_id_type = 'scryfall_id'
          AND e.external_id = ?
        ORDER BY p.printing_id
        LIMIT 1
        """,
        (scryfall_id,),
    ).fetchone()
    card_id = printing["card_id"] if printing else None
    printing_id = printing["printing_id"] if printing else None

    if scan_id:
        claimed = conn.execute(
            """
            INSERT INTO app.scan_log (scan_id, game_id)
            VALUES (?, 'mtg')
            ON CONFLICT (scan_id) DO NOTHING
            RETURNING scan_id
            """,
            (scan_id,),
        ).fetchone()
        if not claimed:
            seen = conn.execute(
                "SELECT row_id, quantity FROM app.scan_log WHERE scan_id = ?",
                (scan_id,),
            ).fetchone()
            if seen and seen["row_id"] is not None:
                return {
                    "success": True,
                    "id": int(seen["row_id"]),
                    "quantity": int(seen["quantity"] or 0),
                    "owner": owner,
                    "finish": finish,
                    "name": name,
                    "duplicate": True,
                }
            raise RuntimeError("Scan idempotency record exists without a saved result")

    # Match SQLite's BEGIN IMMEDIATE behavior for simultaneous first scans:
    # serialize scans that target the same owner/printing/finish/location/flag,
    # including when no inventory row exists yet to lock with FOR UPDATE.
    inventory_lock_key = "\0".join(
        (
            "mtg",
            scryfall_id,
            owner.casefold(),
            finish.casefold(),
            location.casefold(),
            "1" if for_vending else "0",
        )
    )
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(?, 0))",
        (inventory_lock_key,),
    )

    existing = conn.execute(
        """
        SELECT id, quantity
        FROM app.inventory
        WHERE game_id = 'mtg'
          AND scryfall_id = ?
          AND owner = ?
          AND COALESCE(NULLIF(finish, ''), 'normal') = ?
          AND COALESCE(location, 'Free') = ?
          AND deck_id IS NULL
          AND COALESCE(for_vending, FALSE) = ?
        ORDER BY id
        LIMIT 1
        FOR UPDATE
        """,
        (scryfall_id, owner, finish, location, for_vending),
    ).fetchone()

    if existing:
        new_quantity = int(existing["quantity"] or 0) + quantity
        row_id = int(existing["id"])
        conn.execute(
            """
            UPDATE app.inventory
            SET quantity = ?, scan_time = ?, price = ?,
                collector_number = COALESCE(?, collector_number),
                tcgplayer_id = COALESCE(?, tcgplayer_id),
                person_id = ?
            WHERE id = ? AND game_id = 'mtg'
            """,
            (
                new_quantity, scan_time, price, collector_number,
                tcgplayer_id, person_id, row_id,
            ),
        )
    else:
        inserted = conn.execute(
            """
            INSERT INTO app.inventory (
                game_id, card_id, printing_id, scryfall_id, name,
                set_code, rarity, price, scan_time, quantity, owner,
                person_id, finish, collector_number, tcgplayer_id,
                location, for_vending
            )
            VALUES (
                'mtg', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            RETURNING id
            """,
            (
                card_id, printing_id, scryfall_id, name, set_code, rarity,
                price, scan_time, quantity, owner, person_id, finish,
                collector_number, tcgplayer_id, location, for_vending,
            ),
        ).fetchone()
        row_id = int(inserted["id"])
        new_quantity = quantity

    if scan_id:
        conn.execute(
            "UPDATE app.scan_log SET row_id = ?, quantity = ? WHERE scan_id = ?",
            (row_id, new_quantity, scan_id),
        )
        conn.execute(
            "DELETE FROM app.scan_log WHERE created_at < now() - INTERVAL '30 days'"
        )

    return {
        "success": True,
        "id": row_id,
        "quantity": new_quantity,
        "owner": owner,
        "finish": finish,
        "name": name,
        "location": location,
        "is_vending": for_vending,
    }

def fetch_catalog_revision(conn) -> int:
    row = conn.execute(
        """
        SELECT revision
        FROM catalog.game_catalog_state
        WHERE game_id = 'mtg'
        """
    ).fetchone()
    return int(row["revision"] or 0) if row else 0


def fetch_catalog_card_names(conn) -> tuple[int, list[str]]:
    """Return the MTG card-name dictionary and its catalog revision."""
    revision = fetch_catalog_revision(conn)
    rows = conn.execute(
        """
        SELECT name
        FROM (
            SELECT DISTINCT BTRIM(c.name) AS name
            FROM catalog.cards AS c
            WHERE c.game_id = 'mtg'
              AND BTRIM(c.name) <> ''

            UNION

            SELECT DISTINCT BTRIM(p.metadata->>'name') AS name
            FROM catalog.printings AS p
            WHERE p.game_id = 'mtg'
              AND p.card_id IS NULL
              AND BTRIM(COALESCE(p.metadata->>'name', '')) <> ''
        ) AS names
        ORDER BY LOWER(name), name
        """
    ).fetchall()
    return revision, [str(row["name"]) for row in rows]


def fetch_catalog_set_codes(conn) -> tuple[int, set[str]]:
    """Return known MTG set codes and their catalog revision."""
    revision = fetch_catalog_revision(conn)
    rows = conn.execute(
        """
        SELECT set_code
        FROM catalog.sets
        WHERE game_id = 'mtg'
          AND set_code IS NOT NULL
          AND BTRIM(set_code) <> ''
        ORDER BY LOWER(set_code), set_code
        """
    ).fetchall()
    return revision, {str(row["set_code"]).strip().lower() for row in rows}


def fetch_mobile_catalog_search_results(
    conn,
    name: str,
    set_code: str = "",
    collector_number: str = "",
    limit: int = 40,
) -> list[dict]:
    """Search normalized MTG printings for the mobile scanner, without network calls."""
    name = str(name or "").strip()[:120]
    set_code = str(set_code or "").strip().lower()[:12]
    collector_number = str(collector_number or "").strip()[:40]
    limit = max(1, min(int(limit), 40))

    if name and (len(name) < 2 or not any(len(token) >= 2 for token in name.split())):
        return []
    if not name and not (set_code and collector_number):
        return []

    where = [
        "e.game_id = 'mtg'",
        "e.provider_id = 'scryfall'",
        "e.entity_type = 'printing'",
        "e.external_id_type = 'scryfall_id'",
    ]
    params = []
    if name:
        where.append(
            "STRPOS(LOWER(COALESCE(c.name, p.metadata->>'name', '')), LOWER(?)) > 0"
        )
        params.append(name)
    if set_code:
        where.append("LOWER(COALESCE(s.set_code, '')) = ?")
        params.append(set_code)
    if collector_number:
        normalized = collector_number
        if "/" in normalized:
            normalized = normalized.split("/", 1)[0]
        normalized = normalized.lstrip("0") or "0"
        where.append(
            "LOWER(BTRIM(COALESCE(p.collector_number, ''))) IN (?, ?)"
        )
        params.extend([collector_number.lower(), normalized.lower()])

    rank_sql = (
        """
        CASE
            WHEN LOWER(COALESCE(c.name, p.metadata->>'name', '')) = LOWER(?) THEN 0
            WHEN STRPOS(LOWER(COALESCE(c.name, p.metadata->>'name', '')), LOWER(?)) = 1 THEN 1
            ELSE 2
        END
        """
        if name
        else "0"
    )
    rank_params = [name, name] if name else []
    params = rank_params + params + [limit]
    rows = conn.execute(
        f"""
        SELECT
            e.external_id AS scryfall_id,
            COALESCE(c.name, p.metadata->>'name') AS name,
            s.set_code,
            p.rarity,
            COALESCE(
                NULLIF(m.target_external_product_id, ''),
                NULLIF(p.metadata->>'tcgplayer_id', '')
            ) AS tcgplayer_id,
            p.collector_number,
            s.name AS set_name,
            COALESCE(
                m.finishes,
                p.metadata->'finishes',
                '[]'::jsonb
            ) AS finishes,
            {rank_sql} AS match_rank
        FROM catalog.external_ids AS e
        JOIN catalog.printings AS p
          ON p.printing_id = e.printing_id
         AND p.game_id = e.game_id
        LEFT JOIN catalog.cards AS c
          ON c.card_id = p.card_id
         AND c.game_id = p.game_id
        LEFT JOIN catalog.sets AS s
          ON s.set_id = p.set_id
         AND s.game_id = p.game_id
        LEFT JOIN catalog.printing_product_mappings AS m
          ON m.game_id = e.game_id
         AND m.printing_id = p.printing_id
         AND m.source_provider_id = 'scryfall'
         AND m.source_external_printing_id = e.external_id
         AND m.target_provider_id = 'tcgcsv'
        WHERE {' AND '.join(where)}
        ORDER BY
            match_rank,
            LOWER(COALESCE(c.name, p.metadata->>'name', '')),
            LOWER(COALESCE(s.set_code, '')),
            LOWER(COALESCE(p.collector_number, '')),
            e.external_id
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()

    results = []
    for row in rows:
        finishes = row["finishes"]
        if not isinstance(finishes, list):
            finishes = []
        results.append({
            "scryfall_id": str(row["scryfall_id"] or ""),
            "name": row["name"],
            "set_code": str(row["set_code"] or "").lower(),
            "rarity": row["rarity"],
            "tcgplayer_id": str(row["tcgplayer_id"]).strip() if row["tcgplayer_id"] is not None else None,
            "collector_number": row["collector_number"],
            "set_name": row["set_name"],
            "finishes": [str(value).lower() for value in finishes],
        })
    return results

