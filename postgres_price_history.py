"""PostgreSQL persistence and queries for TCGCSV product price history."""

from __future__ import annotations

import json
from datetime import datetime, timezone


def _tcg_product_id(value) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return str(int(float(raw)))
    except (TypeError, ValueError, OverflowError):
        return raw


def _product_id_expression(alias: str = "i") -> str:
    return """COALESCE(
        NULLIF(BTRIM({alias}.tcgplayer_id), ''),
        NULLIF(BTRIM(m.target_external_product_id), ''),
        NULLIF(BTRIM(p.metadata->>'tcgplayer_id'), '')
    )""".format(alias=alias)


def fetch_owned_products(conn, *, vending_only: bool = False) -> list[dict]:
    """Aggregate MTG inventory by resolved TCGCSV product ID."""
    product_id = _product_id_expression()
    vending_filter = "AND COALESCE(i.for_vending, FALSE) IS TRUE" if vending_only else ""
    rows = conn.execute(
        f"""
        SELECT
            {product_id} AS tcgplayer_id,
            MAX(i.scryfall_id) AS scryfall_id,
            MAX(i.name) AS name,
            MAX(i.set_code) AS set_code,
            MAX(i.collector_number) AS collector_number,
            SUM(
                CASE WHEN COALESCE(NULLIF(i.finish, ''), 'normal') = 'normal'
                     THEN COALESCE(i.quantity, 1) ELSE 0 END
            ) AS normal_qty,
            SUM(
                CASE WHEN COALESCE(NULLIF(i.finish, ''), 'normal') = 'foil'
                     THEN COALESCE(i.quantity, 1) ELSE 0 END
            ) AS foil_qty
        FROM app.inventory AS i
        LEFT JOIN catalog.external_ids AS e
          ON e.game_id = i.game_id
         AND e.provider_id = 'scryfall'
         AND e.entity_type = 'printing'
         AND e.external_id_type = 'scryfall_id'
         AND e.external_id = i.scryfall_id
        LEFT JOIN catalog.printings AS p
          ON p.game_id = i.game_id
         AND p.printing_id = COALESCE(i.printing_id, e.printing_id)
        LEFT JOIN catalog.printing_product_mappings AS m
          ON m.game_id = i.game_id
         AND m.printing_id = p.printing_id
         AND m.source_provider_id = 'scryfall'
         AND m.source_external_printing_id = COALESCE(e.external_id, i.scryfall_id)
         AND m.target_provider_id = 'tcgcsv'
        WHERE i.game_id = 'mtg'
          {vending_filter}
          AND {product_id} IS NOT NULL
          AND BTRIM({product_id}) <> ''
        GROUP BY {product_id}
        ORDER BY {product_id}
        """
    ).fetchall()
    return [dict(row) for row in rows]


def has_owned_product(conn, tcg_id: str, *, vending_only: bool = False) -> bool:
    """Check whether the caller may inspect history for an owned product."""
    product_id = _product_id_expression()
    vending_filter = "AND COALESCE(i.for_vending, FALSE) IS TRUE" if vending_only else ""
    row = conn.execute(
        f"""
        SELECT 1
        FROM app.inventory AS i
        LEFT JOIN catalog.external_ids AS e
          ON e.game_id = i.game_id
         AND e.provider_id = 'scryfall'
         AND e.entity_type = 'printing'
         AND e.external_id_type = 'scryfall_id'
         AND e.external_id = i.scryfall_id
        LEFT JOIN catalog.printings AS p
          ON p.game_id = i.game_id
         AND p.printing_id = COALESCE(i.printing_id, e.printing_id)
        LEFT JOIN catalog.printing_product_mappings AS m
          ON m.game_id = i.game_id
         AND m.printing_id = p.printing_id
         AND m.source_provider_id = 'scryfall'
         AND m.source_external_printing_id = COALESCE(e.external_id, i.scryfall_id)
         AND m.target_provider_id = 'tcgcsv'
        WHERE i.game_id = 'mtg'
          AND {product_id} = ?
          {vending_filter}
        LIMIT 1
        """,
        (_tcg_product_id(tcg_id),),
    ).fetchone()
    return row is not None


def fetch_price_history(conn, tcg_id: str, days: int) -> list[dict]:
    """Return the chart-compatible normal/foil series for one MTG TCGCSV product."""
    rows = conn.execute(
        """
        SELECT captured_at, normal_price, foil_price, source_updated
        FROM catalog.product_price_history
        WHERE game_id = 'mtg'
          AND provider_id = 'tcgcsv'
          AND external_product_id = ?
          AND captured_at >= now() - (? * INTERVAL '1 day')
        ORDER BY captured_at ASC, id ASC
        """,
        (_tcg_product_id(tcg_id), max(1, int(days))),
    ).fetchall()
    return [
        {
            "captured_at": row["captured_at"].isoformat()
            if hasattr(row["captured_at"], "isoformat")
            else str(row["captured_at"]),
            "normal": float(row["normal_price"]) if row["normal_price"] is not None else None,
            "foil": float(row["foil_price"]) if row["foil_price"] is not None else None,
            "source_updated": str(row["source_updated"]),
        }
        for row in rows
    ]


def latest_history_price(conn, tcg_id: str, finish: str, cutoff: datetime):
    """Get the latest known finish price at or before a timestamp."""
    column = "foil_price" if finish == "foil" else "normal_price"
    row = conn.execute(
        f"""
        SELECT {column} AS price
        FROM catalog.product_price_history
        WHERE game_id = 'mtg'
          AND provider_id = 'tcgcsv'
          AND external_product_id = ?
          AND captured_at <= ?
          AND {column} IS NOT NULL
        ORDER BY captured_at DESC, id DESC
        LIMIT 1
        """,
        (_tcg_product_id(tcg_id), cutoff),
    ).fetchone()
    return float(row["price"]) if row and row["price"] is not None else None


def recently_alerted(conn, tcg_id: str, finish: str, cutoff: datetime) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM app.price_alert_log
        WHERE game_id = 'mtg'
          AND provider_id = 'tcgcsv'
          AND external_product_id = ?
          AND finish = ?
          AND alerted_at >= ?
        LIMIT 1
        """,
        (_tcg_product_id(tcg_id), finish, cutoff),
    ).fetchone()
    return row is not None


def insert_price_snapshot(
    conn,
    tcg_id: str,
    source_updated: str,
    captured_at: datetime,
    normal_price,
    foil_price,
) -> bool:
    row = conn.execute(
        """
        INSERT INTO catalog.product_price_history (
            game_id, provider_id, external_product_id, captured_at,
            source_updated, normal_price, foil_price
        )
        VALUES ('mtg', 'tcgcsv', ?, ?, ?, ?, ?)
        ON CONFLICT (
            game_id, provider_id, external_product_id, source_updated
        ) DO NOTHING
        RETURNING id
        """,
        (
            _tcg_product_id(tcg_id),
            captured_at,
            str(source_updated),
            normal_price,
            foil_price,
        ),
    ).fetchone()
    return row is not None


def insert_price_observations(
    conn,
    source_rows: list[dict],
    source_updated: str,
    captured_at: datetime,
) -> int:
    """Persist exact TCGCSV subtype rows without collapsing finish variants."""
    inserted = 0
    for item in source_rows:
        if not isinstance(item, dict):
            continue
        product_id = _tcg_product_id(item.get("productId"))
        subtype = str(item.get("subTypeName") or "").strip()
        if not product_id or not subtype:
            continue

        def number(key):
            value = item.get(key)
            if value in (None, ""):
                return None
            try:
                return float(value)
            except (TypeError, ValueError, OverflowError):
                return None

        row = conn.execute(
            """
            INSERT INTO catalog.provider_price_observations (
                game_id, provider_id, external_product_id, subtype_name,
                source_updated, captured_at, market_price, low_price,
                mid_price, high_price, direct_low_price, source_payload
            )
            VALUES (
                'mtg', 'tcgcsv', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?::jsonb
            )
            ON CONFLICT (
                game_id, provider_id, external_product_id,
                subtype_name, source_updated
            ) DO NOTHING
            RETURNING external_product_id
            """,
            (
                product_id,
                subtype,
                str(source_updated),
                captured_at,
                number("marketPrice"),
                number("lowPrice"),
                number("midPrice"),
                number("highPrice"),
                number("directLowPrice"),
                json.dumps(item, ensure_ascii=False, separators=(",", ":")),
            ),
        ).fetchone()
        if row:
            inserted += 1
    return inserted


def insert_price_alert(
    conn,
    *,
    tcg_id: str,
    finish: str,
    alerted_at: datetime,
    old_price: float,
    new_price: float,
    percent_change: float,
    owned_quantity: int,
    owned_value_gain: float,
) -> None:
    conn.execute(
        """
        INSERT INTO app.price_alert_log (
            game_id, provider_id, external_product_id, finish, alerted_at,
            old_price, new_price, percent_change, owned_quantity,
            owned_value_gain
        )
        VALUES ('mtg', 'tcgcsv', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _tcg_product_id(tcg_id),
            finish,
            alerted_at,
            old_price,
            new_price,
            percent_change,
            int(owned_quantity),
            float(owned_value_gain),
        ),
    )


def prune_price_history(conn, cutoff: datetime) -> None:
    conn.execute(
        """
        DELETE FROM catalog.product_price_history
        WHERE game_id = 'mtg'
          AND provider_id = 'tcgcsv'
          AND captured_at < ?
        """,
        (cutoff,),
    )
    conn.execute(
        """
        DELETE FROM catalog.provider_price_observations
        WHERE game_id = 'mtg'
          AND provider_id = 'tcgcsv'
          AND captured_at < ?
        """,
        (cutoff,),
    )
    conn.execute(
        """
        DELETE FROM app.price_alert_log
        WHERE game_id = 'mtg'
          AND provider_id = 'tcgcsv'
          AND alerted_at < ?
        """,
        (cutoff,),
    )
