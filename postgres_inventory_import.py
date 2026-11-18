"""PostgreSQL persistence for standard and ManaBox inventory CSV imports."""

from __future__ import annotations

from datetime import datetime, timezone


def _scan_timestamp(value):
    """Normalize an imported timestamp to an aware UTC-compatible datetime."""
    raw = str(value or "").strip()
    if not raw:
        return datetime.now(timezone.utc)

    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"Invalid scan date/time {raw!r}; use an ISO 8601 date or timestamp."
        ) from exc

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def import_inventory_csv(
    conn,
    reader,
    *,
    owner_override,
    location_override,
    parse_quantity,
    normalize_finish,
    parse_foil_flag,
):
    """Import CSV rows in the caller's transaction.

    The caller is responsible for committing or rolling back the transaction.
    Unknown/inactive owners and blank card names are skipped, matching the
    legacy importer. Rows are inserted individually as inventory entries;
    duplicates are intentionally not merged.
    """
    def get(row, *keys, default=None):
        lowered = {
            str(key).strip().casefold(): value
            for key, value in row.items()
            if key is not None
        }
        for key in keys:
            value = lowered.get(key.casefold())
            if value not in (None, ""):
                return value
        return default

    headers = {
        str(header).strip().casefold()
        for header in (reader.fieldnames or [])
        if header
    }
    is_manabox = bool(
        headers
        & {
            "manabox id",
            "binder name",
            "binder type",
            "purchase price currency",
        }
    ) or ("set code" in headers and "foil" in headers)

    people = conn.execute(
        """
        SELECT id, name
        FROM app.people
        WHERE active IS TRUE
        ORDER BY id
        """
    ).fetchall()
    person_ids = {
        str(person["name"]).strip().casefold(): int(person["id"])
        for person in people
    }

    owner_override = str(owner_override or "").strip()[:100]
    location_override = str(location_override or "").strip()[:120]

    prepared = []
    unknown_owners = set()
    skipped = 0

    for row in reader:
        raw_name = get(row, "name", "card name")
        name = str(raw_name or "").strip()
        if not name:
            skipped += 1
            continue

        owner_value = owner_override or str(
            get(row, "owner", default="Me") or "Me"
        ).strip()[:100] or "Me"
        owner = owner_value.strip()[:100] or "Me"
        person_id = person_ids.get(owner.casefold())
        if person_id is None:
            unknown_owners.add(owner)
            skipped += 1
            continue

        scryfall_id = str(
            get(row, "scryfall_id", "scryfall id", "uuid", default="") or ""
        ).strip() or None
        set_code = str(
            get(row, "set_code", "set code", "set", default="") or ""
        ).strip().upper()
        rarity = str(get(row, "rarity", default="Common") or "Common").strip()
        if rarity.islower():
            rarity = rarity.title()

        price = str(
            get(
                row,
                "price",
                "scanned price",
                "scanned_price",
                "purchase price",
                default="0.00",
            )
            or "0.00"
        )

        foil_flag = parse_foil_flag(
            get(row, "foil", "is foil", "is_foil", "isFoil")
        )
        if foil_flag is not None:
            finish = "foil" if foil_flag else "normal"
        else:
            finish = normalize_finish(
                get(row, "finish", "printing", "treatment", default="normal")
            )

        scan_time = _scan_timestamp(
            get(
                row,
                "scan_date",
                "scan_time",
                "scan time",
                "date",
                "added",
                default=None,
            )
        )
        quantity = parse_quantity(get(row, "quantity", "qty", default=1))

        collector_number = str(
            get(
                row,
                "collector_number",
                "collector number",
                "card number",
                "number",
                default="",
            )
            or ""
        ).strip() or None
        tcgplayer_id = str(
            get(
                row,
                "tcgplayer_id",
                "tcgplayer id",
                "tcgplayer product id",
                "product id",
                default="",
            )
            or ""
        ).strip() or None

        location = location_override or str(
            get(
                row,
                "location",
                "where",
                "binder name",
                default="Free",
            )
            or "Free"
        ).strip()[:120] or "Free"

        raw_vending = get(row, "for_vending", "for vending", "vending")
        for_vending = (
            False
            if raw_vending is None
            else str(raw_vending).strip().casefold()
            in {"1", "true", "yes", "y", "on", "available"}
        )

        prepared.append(
            {
                "scryfall_id": scryfall_id,
                "name": name,
                "set_code": set_code,
                "rarity": rarity,
                "price": price,
                "scan_time": scan_time,
                "quantity": quantity,
                "owner": owner,
                "person_id": person_id,
                "finish": finish,
                "collector_number": collector_number,
                "tcgplayer_id": tcgplayer_id,
                "location": location,
                "for_vending": for_vending,
            }
        )

    # Resolve all known Scryfall printing IDs in batches. Missing catalog
    # entries remain valid inventory rows with nullable card/printing IDs.
    unique_ids = list(
        dict.fromkeys(
            row["scryfall_id"]
            for row in prepared
            if row["scryfall_id"]
        )
    )
    catalog_ids = {}

    for start in range(0, len(unique_ids), 500):
        batch = unique_ids[start : start + 500]
        placeholders = ", ".join("?" for _ in batch)
        matches = conn.execute(
            f"""
            SELECT e.external_id, p.card_id, e.printing_id
            FROM catalog.external_ids AS e
            JOIN catalog.printings AS p
              ON p.printing_id = e.printing_id
             AND p.game_id = e.game_id
            WHERE e.game_id = 'mtg'
              AND e.provider_id = 'scryfall'
              AND e.entity_type = 'printing'
              AND e.external_id_type = 'scryfall_id'
              AND e.external_id IN ({placeholders})
            ORDER BY e.external_id, e.printing_id
            """,
            tuple(batch),
        ).fetchall()

        for match in matches:
            catalog_ids.setdefault(
                str(match["external_id"]),
                (match["card_id"], match["printing_id"]),
            )

    if prepared:
        insert_rows = []
        for row in prepared:
            card_id, printing_id = catalog_ids.get(
                row["scryfall_id"], (None, None)
            )
            insert_rows.append(
                (
                    card_id,
                    printing_id,
                    row["scryfall_id"],
                    row["name"],
                    row["set_code"],
                    row["rarity"],
                    row["price"],
                    row["scan_time"],
                    row["quantity"],
                    row["owner"],
                    row["person_id"],
                    row["finish"],
                    row["collector_number"],
                    row["tcgplayer_id"],
                    row["location"],
                    row["for_vending"],
                )
            )

        conn.executemany(
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
                for_vending
            )
            VALUES (
                'mtg', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            insert_rows,
        )

    return {
        "count": len(prepared),
        "skipped": skipped,
        "unknown_owners": sorted(unknown_owners),
        "format": "manabox" if is_manabox else "standard",
    }
