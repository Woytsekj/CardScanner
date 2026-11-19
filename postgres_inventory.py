"""PostgreSQL-backed inventory read queries for the main collection page."""

from __future__ import annotations

import re
from typing import Any


# Resolve legacy Scryfall identifiers to canonical catalog printings/cards.
# All joins are game-scoped and provider-scoped.
_INVENTORY_JOINS = """
    LEFT JOIN LATERAL (
        SELECT e.printing_id
        FROM catalog.external_ids AS e
        WHERE e.game_id = i.game_id
          AND e.provider_id = 'scryfall'
          AND e.entity_type = 'printing'
          AND e.external_id_type = 'scryfall_id'
          AND e.external_id = i.scryfall_id
        ORDER BY e.external_id_id
        LIMIT 1
    ) AS resolved ON TRUE

    LEFT JOIN catalog.printings AS p
      ON p.game_id = i.game_id
     AND p.printing_id = COALESCE(i.printing_id, resolved.printing_id)

    LEFT JOIN catalog.cards AS c
      ON c.game_id = i.game_id
     AND c.card_id = COALESCE(i.card_id, p.card_id)

    LEFT JOIN catalog.sets AS s
      ON s.game_id = i.game_id
     AND s.set_id = p.set_id

    LEFT JOIN catalog.provider_printing_metadata AS pm
      ON pm.game_id = i.game_id
     AND pm.provider_id = 'scryfall'
     AND pm.printing_id = COALESCE(i.printing_id, p.printing_id)

    LEFT JOIN LATERAL (
        SELECT m.target_external_product_id
        FROM catalog.printing_product_mappings AS m
        WHERE m.game_id = i.game_id
          AND m.printing_id = COALESCE(i.printing_id, p.printing_id)
          AND m.source_provider_id = 'scryfall'
          AND m.target_provider_id = 'tcgcsv'
        ORDER BY m.resolved_at DESC NULLS LAST
        LIMIT 1
    ) AS price_mapping ON TRUE

    LEFT JOIN LATERAL (
        SELECT
            COALESCE(
                NULLIF(pm.type_line, ''),
                NULLIF(c.type_line, ''),
                NULLIF(c.metadata #>> '{legacy_metadata,type_line}', '')
            ) AS type_line,

            COALESCE(
                NULLIF(pm.colors_text, ''),
                NULLIF(c.metadata #>> '{legacy_metadata,colors_text}', ''),
                (
                    SELECT STRING_AGG(color_value, '' ORDER BY ordinal)
                    FROM JSONB_ARRAY_ELEMENTS_TEXT(
                        CASE
                            WHEN JSONB_TYPEOF(pm.colors) = 'array'
                            THEN pm.colors
                            ELSE '[]'::jsonb
                        END
                    ) WITH ORDINALITY AS color_values(color_value, ordinal)
                )
            ) AS colors_text,

            CASE
                WHEN COALESCE(
                    NULLIF(pm.mana_value::text, ''),
                    NULLIF(c.metadata #>> '{legacy_metadata,mana_value}', ''),
                    NULLIF(c.metadata #>> '{legacy_metadata,cmc}', '')
                ) ~ '^-?[0-9]+([.][0-9]+)?$'
                THEN COALESCE(
                    NULLIF(pm.mana_value::text, ''),
                    NULLIF(c.metadata #>> '{legacy_metadata,mana_value}', ''),
                    NULLIF(c.metadata #>> '{legacy_metadata,cmc}', '')
                )::numeric
                ELSE NULL
            END AS mana_value,

            COALESCE(
                pm.oracle_text,
                c.rules_text,
                c.metadata #>> '{legacy_metadata,oracle_text}',
                ''
            ) AS oracle_text,

            COALESCE(
                NULLIF(pm.external_oracle_id, ''),
                NULLIF(c.metadata->>'oracle_id', ''),
                NULLIF(p.metadata->>'oracle_id', '')
            ) AS oracle_id,

            COALESCE(
                pm.legalities::text,
                c.metadata #>> '{legacy_metadata,legalities_json}',
                '{}'
            ) AS legalities_json,

            pm.updated_at AS updated_at
    ) AS cm ON TRUE
"""


def _tag_name_match_sql(tag_alias: str, value: str) -> tuple[str, list[str]]:
    """Match a tag slug, label, or provider tag alias."""
    pattern = f"%{value.replace('*', '%')}%"
    sql = f"""
        (
            {tag_alias}.slug ILIKE ?
            OR {tag_alias}.label ILIKE ?
            OR EXISTS (
                SELECT 1
                FROM catalog.tag_aliases AS tag_aliases
                WHERE tag_aliases.game_id = {tag_alias}.game_id
                  AND tag_aliases.provider_id = {tag_alias}.provider_id
                  AND tag_aliases.tag_id = {tag_alias}.tag_id
                  AND tag_aliases.alias ILIKE ?
            )
        )
    """
    return sql, [pattern, pattern, pattern]


def parse_postgres_inventory_search(
    search: str,
) -> tuple[list[str], list[Any], str | None, str | None]:
    """Parse the inventory page's supported Scryfall-style search subset."""
    where: list[str] = []
    args: list[Any] = []
    order_override: str | None = None
    direction_override: str | None = None

    token_pattern = re.compile(
        r'-?[A-Za-z][A-Za-z0-9_]*(?:>=|<=|!=|=|:|>|<)"(?:\\.|[^"\\])*"'
        r'|-?[A-Za-z][A-Za-z0-9_]*(?:>=|<=|!=|=|:|>|<)\S+'
        r'|-(?:"(?:\\.|[^"\\])*"|\S+)'
        r'|"(?:\\.|[^"\\])*"|\S+'
    )
    tokens = token_pattern.findall(search or "")
    rarity_rank = {
        "common": 1,
        "uncommon": 2,
        "rare": 3,
        "mythic": 4,
        "mythic rare": 4,
    }

    def append(expression: str, values: list[Any], negated: bool) -> None:
        where.append(f"NOT ({expression})" if negated else f"({expression})")
        args.extend(values)

    for raw in tokens:
        negated = raw.startswith("-")
        token = raw[1:] if negated else raw
        field = None
        operator = None
        value = None

        for op in (">=", "<=", "!=", ":", "=", ">", "<"):
            if op in token:
                possible_field, possible_value = token.split(op, 1)
                if (
                    possible_field
                    and possible_value
                    and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", possible_field)
                ):
                    field = possible_field.lower()
                    operator = op
                    value = possible_value
                    break

        if value is not None:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] == '"':
                value = value[1:-1].replace('\\"', '"').replace('\\\\', '\\')

            if field == "order":
                order_map = {
                    "set": "set",
                    "name": "name",
                    "collector": "number",
                    "number": "number",
                    "usd": "price",
                    "price": "price",
                    "cmc": "cmc",
                    "date": "date",
                    "released": "date",
                }
                order_override = order_map.get(value.lower(), order_override)
                continue

            if field in {"direction", "dir"}:
                if value.lower() in {"asc", "desc"}:
                    direction_override = value.lower()
                continue

            if field in {
                "art", "atag", "arttag",
                "otag", "oracletag", "oracle_tag", "function",
            }:
                safe_value = value.replace("%", "").replace("_", "")
                pattern = f"%{safe_value.replace('*', '%')}%"

                if field in {"art", "atag", "arttag"}:
                    expression = """
                        EXISTS (
                            SELECT 1
                            FROM catalog.art_tag_assignments AS ata
                            JOIN catalog.tags AS st
                              ON st.game_id = ata.game_id
                             AND st.provider_id = ata.provider_id
                             AND st.tag_id = ata.tag_id
                             AND st.tag_type = 'art'
                            JOIN catalog.printing_faces AS af
                              ON af.game_id = ata.game_id
                             AND af.illustration_id = ata.illustration_id
                            JOIN catalog.external_ids AS face_external
                              ON face_external.game_id = af.game_id
                             AND face_external.printing_id = af.printing_id
                             AND face_external.provider_id = 'scryfall'
                             AND face_external.entity_type = 'printing'
                             AND face_external.external_id_type = 'scryfall_id'
                             AND face_external.external_id = i.scryfall_id
                            WHERE ata.game_id = 'mtg'
                              AND ata.provider_id = 'scryfall'
                              AND ata.tag_type = 'art'
                              AND
                    """
                else:
                    expression = """
                        EXISTS (
                            SELECT 1
                            FROM catalog.oracle_tag_assignments AS ota
                            JOIN catalog.tags AS st
                              ON st.game_id = ota.game_id
                             AND st.provider_id = ota.provider_id
                             AND st.tag_id = ota.tag_id
                             AND st.tag_type = 'oracle'
                            WHERE ota.game_id = 'mtg'
                              AND ota.provider_id = 'scryfall'
                              AND ota.tag_type = 'oracle'
                              AND (
                                  ota.external_oracle_id = cm.oracle_id
                                  OR ota.card_id = c.card_id
                              )
                              AND
                    """

                tag_match, tag_args = _tag_name_match_sql("st", value)
                append(expression + tag_match + "\n)", tag_args, negated or operator == "!=")
                continue

            if field in {"set", "s"}:
                pattern = value.replace("*", "%")
                if "%" in pattern or "_" in pattern:
                    append("COALESCE(i.set_code, s.set_code, '') ILIKE ?", [pattern], negated)
                else:
                    append("UPPER(COALESCE(i.set_code, s.set_code, '')) = ?", [value.upper()], negated)
                continue

            if field in {"cn", "collector", "collector_number"}:
                expression = "COALESCE(i.collector_number, p.collector_number, '')"
                pattern = value.replace("*", "%")
                if "%" in pattern or "_" in pattern:
                    append(f"{expression} ILIKE ?", [pattern], negated)
                else:
                    append(f"{expression} = ?", [value], negated)
                continue

            if field in {"name", "card"}:
                append("i.name ILIKE ?", [f"%{value}%"], negated)
                continue

            if field in {"t", "type"}:
                parts = [part for part in re.split(r"\s+", value) if part]
                expression = " AND ".join(
                    "COALESCE(cm.type_line, '') ILIKE ?" for _ in parts
                ) or "TRUE"
                append(expression, [f"%{part}%" for part in parts], negated)
                continue

            if field in {"o", "oracle"}:
                pattern = f"%{value.replace('*', '%')}%"
                append("COALESCE(cm.oracle_text, '') ILIKE ?", [pattern], negated)
                continue

            if field in {"c", "color", "colors"}:
                color_value = value.lower().replace(" ", "")
                if color_value in {"colorless", "c"}:
                    expression, values = "COALESCE(cm.colors_text, '') = ''", []
                elif color_value in {"multicolor", "multi"}:
                    expression, values = "CHAR_LENGTH(COALESCE(cm.colors_text, '')) > 1", []
                else:
                    symbols = [char.upper() for char in color_value if char.upper() in "WUBRG"]
                    if not symbols:
                        expression, values = "FALSE", []
                    elif operator in {"=", "!="}:
                        expression = "UPPER(COALESCE(cm.colors_text, '')) = ?"
                        values = ["".join(char for char in "WUBRG" if char in symbols)]
                    else:
                        expression = " AND ".join(
                            "UPPER(COALESCE(cm.colors_text, '')) LIKE ?" for _ in symbols
                        )
                        values = [f"%{char}%" for char in symbols]
                append(expression, values, negated or operator == "!=")
                continue

            if field in {"r", "rarity"}:
                rank_expression = (
                    "CASE LOWER(COALESCE(i.rarity, p.rarity, '')) "
                    "WHEN 'common' THEN 1 WHEN 'uncommon' THEN 2 "
                    "WHEN 'rare' THEN 3 WHEN 'mythic' THEN 4 "
                    "WHEN 'mythic rare' THEN 4 ELSE 0 END"
                )
                rarity = rarity_rank.get(value.lower())
                if operator in {">=", "<=", ">", "<", "=", "!="} and rarity is not None:
                    append(f"{rank_expression} {operator} ?", [rarity], negated)
                elif operator in {":", "="}:
                    append("LOWER(COALESCE(i.rarity, p.rarity, '')) = ?", [value.lower()], negated)
                else:
                    append("FALSE", [], negated)
                continue

            if field == "is":
                flag = value.lower()
                if flag in {"foil", "etched"}:
                    append(
                        "LOWER(COALESCE(i.finish, 'normal')) IN "
                        "('foil','etched','etched foil','foil etched')",
                        [],
                        negated,
                    )
                elif flag in {"nonfoil", "normal"}:
                    append(
                        "LOWER(COALESCE(i.finish, 'normal')) NOT IN "
                        "('foil','etched','etched foil','foil etched')",
                        [],
                        negated,
                    )
                else:
                    needle = f"%{token}%"
                    append(
                        "(i.name ILIKE ? OR COALESCE(i.collector_number, p.collector_number, '') ILIKE ?)",
                        [needle, needle],
                        negated,
                    )
                continue

            if field == "owner":
                append("COALESCE(i.owner, 'Me') ILIKE ?", [value], negated or operator == "!=")
                continue

            if field in {"loc", "location"}:
                append("COALESCE(i.location, 'Free') ILIKE ?", [value], negated or operator == "!=")
                continue

            if field == "deck":
                if value.lower() in {"none", "unassigned", "free"}:
                    append("i.deck_id IS NOT NULL", [], not (negated or operator == "!="))
                else:
                    append(
                        """
                        i.deck_id = (
                            SELECT d.id
                            FROM app.decklists AS d
                            WHERE d.game_id = 'mtg'
                              AND d.name ILIKE ?
                            ORDER BY d.updated_at DESC
                            LIMIT 1
                        )
                        """,
                        [value],
                        negated or operator == "!=",
                    )
                continue

            if field == "vending":
                want = value.lower() in {"1", "true", "yes", "on", "available"}
                append(
                    "COALESCE(i.for_vending, FALSE) = ?",
                    [want],
                    negated or operator == "!=",
                )
                continue

            needle = f"%{token}%"
            append(
                """
                (
                    i.name ILIKE ?
                    OR COALESCE(i.collector_number, p.collector_number, '') ILIKE ?
                    OR COALESCE(i.set_code, s.set_code, '') ILIKE ?
                )
                """,
                [needle, needle, needle],
                negated,
            )
            continue

        plain = token.strip('"')
        if plain:
            needle = f"%{plain}%"
            append(
                """
                (
                    i.name ILIKE ?
                    OR COALESCE(i.collector_number, p.collector_number, '') ILIKE ?
                    OR EXISTS (
                        SELECT 1
                        FROM app.card_aliases AS ca
                        WHERE ca.game_id = i.game_id
                          AND ca.provider_id = 'scryfall'
                          AND ca.external_printing_id = i.scryfall_id
                          AND ca.alias ILIKE ?
                    )
                )
                """,
                [needle, needle, needle],
                negated,
            )

    return where, args, order_override, direction_override


def _inventory_filters(
    search: str,
    owner_filter: str,
    finish_filter: str,
    type_filter: str = "",
    color_filter: str = "",
    location_filter: str = "",
    vending_filter: str = "",
) -> tuple[list[str], list[Any], str | None, str | None]:
    where, args, query_order, query_direction = parse_postgres_inventory_search(search)

    if owner_filter:
        where.append("COALESCE(i.owner, 'Me') = ?")
        args.append(owner_filter)

    if finish_filter:
        where.append("COALESCE(NULLIF(i.finish, ''), 'normal') = ?")
        args.append(finish_filter)

    if type_filter:
        where.append("COALESCE(cm.type_line, '') ILIKE ?")
        args.append(f"%{type_filter}%")

    if color_filter == "multicolor":
        where.append("CHAR_LENGTH(COALESCE(cm.colors_text, '')) > 1")
    elif color_filter == "colorless":
        where.append("COALESCE(cm.colors_text, '') = ''")
    elif color_filter:
        where.append("COALESCE(cm.colors_text, '') ILIKE ?")
        args.append(f"%{color_filter}%")

    if location_filter:
        where.append("COALESCE(i.location, 'Free') ILIKE ?")
        args.append(location_filter)

    if vending_filter == "yes":
        where.append("COALESCE(i.for_vending, FALSE) = TRUE")
    elif vending_filter == "no":
        where.append("COALESCE(i.for_vending, FALSE) = FALSE")

    return where, args, query_order, query_direction


def fetch_inventory_summary_rows(
    conn,
    search: str,
    owner_filter: str,
    finish_filter: str,
    sort_key: str,
    direction: str,
    type_filter: str = "",
    color_filter: str = "",
    location_filter: str = "",
    vending_filter: str = "",
) -> list[dict]:
    """Return grouped inventory rows used to calculate collection totals."""
    where, args, _, _ = _inventory_filters(
        search,
        owner_filter,
        finish_filter,
        type_filter,
        color_filter,
        location_filter,
        vending_filter,
    )
    where_sql = "WHERE i.game_id = 'mtg'"
    if where:
        where_sql += " AND " + " AND ".join(where)

    owner_expr = "COALESCE(i.owner, 'Me')"
    finish_expr = "COALESCE(NULLIF(i.finish, ''), 'normal')"
    quantity_expr = "GREATEST(COALESCE(i.quantity, 1), 1)"
    tcg_id_expr = """
        COALESCE(
            NULLIF(BTRIM(i.tcgplayer_id), ''),
            NULLIF(BTRIM(price_mapping.target_external_product_id), ''),
            NULLIF(BTRIM(p.metadata->>'tcgplayer_id'), '')
        )
    """

    sql = f"""
        SELECT
            i.scryfall_id,
            {owner_expr} AS owner,
            {finish_expr} AS finish,
            SUM({quantity_expr}) AS quantity,
            COUNT(*) AS row_count,
            i.price AS scanned_price,
            {tcg_id_expr} AS tcgplayer_id
        FROM app.inventory AS i
        {_INVENTORY_JOINS}
        {where_sql}
        GROUP BY
            i.scryfall_id,
            {owner_expr},
            {finish_expr},
            i.price,
            {tcg_id_expr}
    """
    return conn.execute(sql, args).fetchall()


def _count_rows_for_printings(conn, scryfall_ids: list[str] | None = None, include_null: bool = False):
    predicates = []
    args: list[Any] = []

    if scryfall_ids:
        placeholders = ", ".join("?" for _ in scryfall_ids)
        predicates.append(f"scryfall_id IN ({placeholders})")
        args.extend(scryfall_ids)

    if include_null:
        predicates.append("scryfall_id IS NULL")

    where_sql = "WHERE game_id = 'mtg'"
    if predicates:
        where_sql += " AND (" + " OR ".join(predicates) + ")"

    return conn.execute(
        f"""
        SELECT
            scryfall_id,
            COALESCE(owner, 'Me') AS owner,
            COALESCE(NULLIF(finish, ''), 'normal') AS finish,
            SUM(COALESCE(quantity, 1)) AS qty
        FROM app.inventory
        {where_sql}
        GROUP BY
            scryfall_id,
            COALESCE(owner, 'Me'),
            COALESCE(NULLIF(finish, ''), 'normal')
        """,
        args,
    ).fetchall()


def fetch_inventory_page_rows(
    conn,
    search: str,
    owner_filter: str,
    finish_filter: str,
    sort_key: str,
    direction: str,
    type_filter: str = "",
    color_filter: str = "",
    location_filter: str = "",
    vending_filter: str = "",
    limit: int | None = None,
    offset: int = 0,
) -> tuple[list[dict], list[dict], bool]:
    """Return enriched rows and page-wide finish-count data.

    For market-price sorts, the app applies its cached price snapshot in Python,
    so the filtered rows are returned without SQL pagination. Other sorts use
    PostgreSQL LIMIT/OFFSET.
    """
    where, args, query_order, query_direction = _inventory_filters(
        search,
        owner_filter,
        finish_filter,
        type_filter,
        color_filter,
        location_filter,
        vending_filter,
    )

    if query_order:
        sort_key = query_order
    if query_direction:
        direction = query_direction

    sort_key = sort_key if sort_key in {
        "date", "name", "set", "number", "cmc", "quantity",
        "finish", "owner", "scanned", "normal", "foil", "price", "value",
    } else "date"
    direction_sql = "ASC" if direction == "asc" else "DESC"

    price_sort = sort_key in {"normal", "foil", "price", "value"}
    page_limit_in_sql = limit is not None and not price_sort

    if sort_key == "name":
        order_sql = "LOWER(i.name)"
    elif sort_key == "set":
        collector = "COALESCE(i.collector_number, p.collector_number, '0')"
        order_sql = (
            "LOWER(COALESCE(i.set_code, s.set_code, '')), "
            f"CASE WHEN {collector} ~ '^[0-9]+$' "
            f"THEN {collector}::numeric ELSE 0 END, "
            f"LOWER({collector})"
        )
    elif sort_key == "number":
        order_sql = "LOWER(COALESCE(i.collector_number, p.collector_number, ''))"
    elif sort_key == "cmc":
        order_sql = "COALESCE(cm.mana_value, -1)"
    elif sort_key == "quantity":
        order_sql = "COALESCE(i.quantity, 1)"
    elif sort_key == "finish":
        order_sql = "LOWER(COALESCE(NULLIF(i.finish, ''), 'normal'))"
    elif sort_key == "owner":
        order_sql = "LOWER(COALESCE(i.owner, 'Me'))"
    elif sort_key == "scanned":
        order_sql = (
            "CASE WHEN i.price ~ '^-?[0-9]+([.][0-9]+)?$' "
            "THEN i.price::numeric ELSE 0 END"
        )
    else:
        order_sql = "i.scan_time"

    where_sql = "WHERE i.game_id = 'mtg'"
    if where:
        where_sql += " AND " + " AND ".join(where)

    tcg_id_expr = """
        COALESCE(
            NULLIF(BTRIM(i.tcgplayer_id), ''),
            NULLIF(BTRIM(price_mapping.target_external_product_id), ''),
            NULLIF(BTRIM(p.metadata->>'tcgplayer_id'), '')
        )
    """

    query = f"""
        SELECT
            i.id,
            i.scryfall_id,
            i.name,
            COALESCE(i.set_code, s.set_code, p.metadata->>'set_code') AS set_code,
            COALESCE(i.rarity, p.rarity, p.metadata->>'rarity') AS rarity,
            i.price AS scanned_price,
            i.scan_time,
            COALESCE(i.quantity, 1) AS quantity,
            COALESCE(i.owner, 'Me') AS owner,
            COALESCE(NULLIF(i.finish, ''), 'normal') AS finish,
            COALESCE(i.collector_number, p.collector_number) AS collector_number,
            {tcg_id_expr} AS tcgplayer_id,
            COALESCE(i.location, 'Free') AS location,
            i.deck_id,
            COALESCE(i.for_vending, FALSE) AS for_vending,
            cm.type_line,
            cm.colors_text,
            cm.mana_value,
            cm.oracle_text,
            cm.oracle_id,
            cm.legalities_json,
            cm.updated_at AS metadata_updated_at,
            COALESCE((
                SELECT STRING_AGG(ca.alias, ' · ' ORDER BY ca.id)
                FROM app.card_aliases AS ca
                WHERE ca.game_id = i.game_id
                  AND ca.provider_id = 'scryfall'
                  AND ca.external_printing_id = i.scryfall_id
            ), '') AS alternate_names
        FROM app.inventory AS i
        {_INVENTORY_JOINS}
        {where_sql}
        ORDER BY {order_sql} {direction_sql}, i.id ASC
    """

    query_args = list(args)
    safe_limit = max(1, int(limit)) if limit is not None else None
    safe_offset = max(0, int(offset))

    if page_limit_in_sql:
        query += " LIMIT ? OFFSET ?"
        query_args.extend([safe_limit, safe_offset])

    rows = conn.execute(query, query_args).fetchall()

    if page_limit_in_sql:
        page_sids = sorted({
            str(row["scryfall_id"]).strip()
            for row in rows
            if row["scryfall_id"]
        })
        page_has_null_sid = any(not row["scryfall_id"] for row in rows)
        count_rows = _count_rows_for_printings(conn, page_sids, page_has_null_sid)
    elif limit is not None:
        # Price sorting needs quantities from every printing represented in the
        # result set before slicing the requested page.
        count_rows = _count_rows_for_printings(conn)
    else:
        count_rows = _count_rows_for_printings(conn)

    return rows, count_rows, page_limit_in_sql


def fetch_inventory_page_options(conn) -> tuple[list[str], list[dict], int, int, list[str]]:
    """Load the owner/deck/location selectors and inventory-wide counts."""
    owners = [
        row["name"]
        for row in conn.execute("""
            SELECT name
            FROM app.people
            WHERE active IS TRUE
            ORDER BY LOWER(name), name
        """).fetchall()
    ]

    decks = conn.execute("""
        SELECT id, name
        FROM app.decklists
        WHERE game_id = 'mtg'
        ORDER BY LOWER(name), name
    """).fetchall()

    totals = conn.execute("""
        SELECT
            COUNT(*) AS total_rows,
            COALESCE(SUM(COALESCE(quantity, 1)), 0) AS total_cards
        FROM app.inventory
        WHERE game_id = 'mtg'
    """).fetchone()

    locations = [
        row["location"]
        for row in conn.execute("""
            SELECT location
            FROM (
                SELECT DISTINCT COALESCE(location, 'Free') AS location
                FROM app.inventory
                WHERE game_id = 'mtg'
            ) AS inventory_locations
            ORDER BY LOWER(location), location
        """).fetchall()
    ]
    if "Free" not in locations:
        locations.insert(0, "Free")

    return (
        owners,
        decks,
        int(totals["total_rows"] or 0),
        int(totals["total_cards"] or 0),
        locations,
    )
