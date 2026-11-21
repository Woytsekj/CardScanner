"""PostgreSQL persistence and queries for compact TCGCSV price history.

The observations table is a change-event log. provider_price_current tracks the
latest state and last-seen source build, including explicit unavailability.
"""

from __future__ import annotations

from datetime import datetime


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


def _subtype_for_finish(finish: str) -> str:
    return "Normal" if str(finish or "").strip().lower() == "normal" else "Foil"


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


def fetch_current_price(conn, tcg_id: str, subtype_name: str) -> float | None:
    """Return the latest available market price for a product and exact subtype."""
    row = conn.execute(
        """
        SELECT market_price
        FROM catalog.provider_price_current
        WHERE game_id = 'mtg'
          AND provider_id = 'tcgcsv'
          AND external_product_id = ?
          AND subtype_name = ?
          AND is_available IS TRUE
        """,
        (_tcg_product_id(tcg_id), str(subtype_name)),
    ).fetchone()
    return float(row["market_price"]) if row and row["market_price"] is not None else None


def fetch_price_history(conn, tcg_id: str, days: int) -> list[dict]:
    """Return daily reconstructed values for every exact provider subtype."""
    product_id = _tcg_product_id(tcg_id)
    day_count = max(1, min(365, int(days)))
    rows = conn.execute(
        """
        WITH subtypes AS (
            SELECT DISTINCT subtype_name
            FROM catalog.provider_price_observations
            WHERE game_id = 'mtg'
              AND provider_id = 'tcgcsv'
              AND external_product_id = ?
        ),
        sample_days AS (
            SELECT generate_series(
                now() - (?::double precision * INTERVAL '1 day'),
                now(),
                INTERVAL '1 day'
            ) AS sample_at
        )
        SELECT
            subtypes.subtype_name,
            sample_days.sample_at AS captured_at,
            history.source_updated,
            history.is_available,
            history.market_price,
            history.low_price,
            history.mid_price,
            history.high_price,
            history.direct_low_price
        FROM subtypes
        CROSS JOIN sample_days
        LEFT JOIN LATERAL (
            SELECT
                source_updated, is_available, market_price, low_price,
                mid_price, high_price, direct_low_price
            FROM catalog.provider_price_observations
            WHERE game_id = 'mtg'
              AND provider_id = 'tcgcsv'
              AND external_product_id = ?
              AND subtype_name = subtypes.subtype_name
              AND captured_at <= sample_days.sample_at
            ORDER BY captured_at DESC, source_updated DESC
            LIMIT 1
        ) AS history ON TRUE
        ORDER BY subtypes.subtype_name, sample_days.sample_at
        """,
        (product_id, day_count, product_id),
    ).fetchall()

    series: dict[str, dict] = {}
    for row in rows:
        subtype = str(row["subtype_name"])
        item = series.setdefault(subtype, {"subtype_name": subtype, "points": []})
        available = bool(row["is_available"])
        captured_at = row["captured_at"]
        item["points"].append({
            "captured_at": captured_at.isoformat()
            if hasattr(captured_at, "isoformat") else str(captured_at),
            "source_updated": str(row["source_updated"]) if row["source_updated"] else None,
            "available": available,
            "market": float(row["market_price"]) if available and row["market_price"] is not None else None,
            "low": float(row["low_price"]) if available and row["low_price"] is not None else None,
            "mid": float(row["mid_price"]) if available and row["mid_price"] is not None else None,
            "high": float(row["high_price"]) if available and row["high_price"] is not None else None,
            "direct_low": float(row["direct_low_price"]) if available and row["direct_low_price"] is not None else None,
        })
    return list(series.values())


def fetch_latest_price_trends(
    conn,
    product_ids: list[str] | tuple[str, ...],
) -> dict[str, list[dict]]:
    """Return today's and yesterday's reconstructed Normal/Foil market prices."""
    normalized = sorted({
        _tcg_product_id(value)
        for value in product_ids
        if _tcg_product_id(value)
    })
    trends: dict[str, list[dict]] = {}
    for start in range(0, len(normalized), 400):
        chunk = normalized[start:start + 400]
        product_values = ", ".join("?::text" for _ in chunk)
        rows = conn.execute(
            f"""
            WITH selected_products AS (
                SELECT unnest(ARRAY[{product_values}]) AS external_product_id
            ),
            sample_times AS (
                SELECT 1 AS sample_rank, now() AS sample_at
                UNION ALL
                SELECT 2 AS sample_rank, now() - INTERVAL '1 day' AS sample_at
            )
            SELECT
                products.external_product_id,
                samples.sample_rank,
                samples.sample_at AS captured_at,
                CASE WHEN normal_price.is_available THEN normal_price.trend_price END AS normal_price,
                CASE WHEN foil_price.is_available THEN foil_price.trend_price END AS foil_price
            FROM selected_products AS products
            CROSS JOIN sample_times AS samples
            LEFT JOIN LATERAL (
                SELECT COALESCE(market_price, mid_price, low_price) AS trend_price,
                       is_available
                FROM catalog.provider_price_observations
                WHERE game_id = 'mtg'
                  AND provider_id = 'tcgcsv'
                  AND external_product_id = products.external_product_id
                  AND subtype_name = 'Normal'
                  AND captured_at <= samples.sample_at
                ORDER BY captured_at DESC, source_updated DESC
                LIMIT 1
            ) AS normal_price ON TRUE
            LEFT JOIN LATERAL (
                SELECT COALESCE(market_price, mid_price, low_price) AS trend_price,
                       is_available
                FROM catalog.provider_price_observations
                WHERE game_id = 'mtg'
                  AND provider_id = 'tcgcsv'
                  AND external_product_id = products.external_product_id
                  AND subtype_name = 'Foil'
                  AND captured_at <= samples.sample_at
                ORDER BY captured_at DESC, source_updated DESC
                LIMIT 1
            ) AS foil_price ON TRUE
            ORDER BY products.external_product_id, samples.sample_rank
            """,
            tuple(chunk),
        ).fetchall()
        for row in rows:
            product_id = _tcg_product_id(row["external_product_id"])
            trends.setdefault(product_id, []).append({
                "normal_price": float(row["normal_price"]) if row["normal_price"] is not None else None,
                "foil_price": float(row["foil_price"]) if row["foil_price"] is not None else None,
                "captured_at": (
                    row["captured_at"].isoformat()
                    if hasattr(row["captured_at"], "isoformat")
                    else str(row["captured_at"])
                ),
            })
    return trends

def latest_history_price(
    conn,
    tcg_id: str,
    finish: str,
    cutoff: datetime,
    *,
    not_before: datetime | None = None,
):
    """Get the latest available market price for a finish before a cutoff."""
    # For change-event history, a lookback reference asks for the state at
    # the start of the window. The last change may be much older than the
    # window when a price has remained stable, so do not require a recent row.
    as_of = not_before if not_before is not None else cutoff
    params = [_tcg_product_id(tcg_id), _subtype_for_finish(finish), as_of]
    row = conn.execute(
        f"""
        SELECT COALESCE(market_price, mid_price, low_price) AS price, is_available
        FROM catalog.provider_price_observations
        WHERE game_id = 'mtg'
          AND provider_id = 'tcgcsv'
          AND external_product_id = ?
          AND subtype_name = ?
          AND captured_at <= ?
        ORDER BY captured_at DESC, source_updated DESC
        LIMIT 1
        """,
        tuple(params),
    ).fetchone()
    if not row or not row["is_available"] or row["price"] is None:
        return None
    return float(row["price"])


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
    """Legacy compatibility writer; new PostgreSQL syncs use exact observations."""
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


def _number(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def insert_price_observations(
    conn,
    source_rows: list[dict],
    source_updated: str,
    captured_at: datetime,
) -> int:
    """Store only price-state changes and explicit source-row removals.

    A successful full TCGCSV build refreshes current-state last-seen metadata.
    Historical observations are added only for an initial state, a change in
    any of the five metrics, or an unavailable/available transition.
    """
    conn.execute(
        """
        CREATE TEMP TABLE IF NOT EXISTS incoming_tcgcsv_prices (
            external_product_id TEXT NOT NULL,
            subtype_name TEXT NOT NULL,
            market_price NUMERIC,
            low_price NUMERIC,
            mid_price NUMERIC,
            high_price NUMERIC,
            direct_low_price NUMERIC
        ) ON COMMIT DROP
        """
    )
    conn.execute("TRUNCATE TABLE incoming_tcgcsv_prices")

    staged = []
    for item in source_rows or []:
        if not isinstance(item, dict):
            continue
        product_id = _tcg_product_id(item.get("productId"))
        subtype = str(item.get("subTypeName") or "").strip()
        if not product_id or not subtype:
            continue
        staged.append((
            product_id, subtype,
            _number(item.get("marketPrice")),
            _number(item.get("lowPrice")),
            _number(item.get("midPrice")),
            _number(item.get("highPrice")),
            _number(item.get("directLowPrice")),
        ))

    if staged:
        conn.executemany(
            """
            INSERT INTO incoming_tcgcsv_prices (
                external_product_id, subtype_name, market_price, low_price,
                mid_price, high_price, direct_low_price
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            staged,
        )

    changed = conn.execute(
        """
        WITH incoming AS (
            SELECT DISTINCT ON (external_product_id, subtype_name)
                external_product_id, subtype_name, market_price, low_price,
                mid_price, high_price, direct_low_price
            FROM incoming_tcgcsv_prices
            ORDER BY external_product_id, subtype_name
        )
        INSERT INTO catalog.provider_price_observations (
            game_id, provider_id, external_product_id, subtype_name,
            source_updated, captured_at, market_price, low_price,
            mid_price, high_price, direct_low_price, is_available
        )
        SELECT
            'mtg', 'tcgcsv', i.external_product_id, i.subtype_name,
            ?, ?, i.market_price, i.low_price, i.mid_price,
            i.high_price, i.direct_low_price, TRUE
        FROM incoming i
        LEFT JOIN catalog.provider_price_current c
          ON c.game_id = 'mtg'
         AND c.provider_id = 'tcgcsv'
         AND c.external_product_id = i.external_product_id
         AND c.subtype_name = i.subtype_name
        WHERE c.external_product_id IS NULL
           OR c.is_available IS FALSE
           OR ROW(
                c.market_price, c.low_price, c.mid_price,
                c.high_price, c.direct_low_price
              ) IS DISTINCT FROM ROW(
                i.market_price, i.low_price, i.mid_price,
                i.high_price, i.direct_low_price
              )
        ON CONFLICT (
            game_id, provider_id, external_product_id,
            subtype_name, source_updated
        ) DO NOTHING
        RETURNING external_product_id
        """,
        (str(source_updated), captured_at),
    ).fetchall()
    inserted = len(changed)

    conn.execute(
        """
        WITH incoming AS (
            SELECT DISTINCT ON (external_product_id, subtype_name)
                external_product_id, subtype_name, market_price, low_price,
                mid_price, high_price, direct_low_price
            FROM incoming_tcgcsv_prices
            ORDER BY external_product_id, subtype_name
        )
        INSERT INTO catalog.provider_price_current (
            game_id, provider_id, external_product_id, subtype_name,
            source_updated, captured_at, market_price, low_price,
            mid_price, high_price, direct_low_price, is_available
        )
        SELECT
            'mtg', 'tcgcsv', external_product_id, subtype_name,
            ?, ?, market_price, low_price, mid_price,
            high_price, direct_low_price, TRUE
        FROM incoming
        ON CONFLICT (
            game_id, provider_id, external_product_id, subtype_name
        ) DO UPDATE SET
            source_updated = EXCLUDED.source_updated,
            captured_at = EXCLUDED.captured_at,
            market_price = EXCLUDED.market_price,
            low_price = EXCLUDED.low_price,
            mid_price = EXCLUDED.mid_price,
            high_price = EXCLUDED.high_price,
            direct_low_price = EXCLUDED.direct_low_price,
            is_available = TRUE
        """,
        (str(source_updated), captured_at),
    )

    removed = conn.execute(
        """
        WITH missing AS (
            UPDATE catalog.provider_price_current AS c
            SET source_updated = ?,
                captured_at = ?,
                market_price = NULL,
                low_price = NULL,
                mid_price = NULL,
                high_price = NULL,
                direct_low_price = NULL,
                is_available = FALSE
            WHERE c.game_id = 'mtg'
              AND c.provider_id = 'tcgcsv'
              AND c.is_available IS TRUE
              AND NOT EXISTS (
                  SELECT 1
                  FROM incoming_tcgcsv_prices i
                  WHERE i.external_product_id = c.external_product_id
                    AND i.subtype_name = c.subtype_name
              )
            RETURNING external_product_id, subtype_name
        )
        INSERT INTO catalog.provider_price_observations (
            game_id, provider_id, external_product_id, subtype_name,
            source_updated, captured_at, market_price, low_price,
            mid_price, high_price, direct_low_price, is_available
        )
        SELECT
            'mtg', 'tcgcsv', external_product_id, subtype_name,
            ?, ?, NULL, NULL, NULL, NULL, NULL, FALSE
        FROM missing
        ON CONFLICT (
            game_id, provider_id, external_product_id,
            subtype_name, source_updated
        ) DO NOTHING
        RETURNING external_product_id
        """,
        (str(source_updated), captured_at, str(source_updated), captured_at),
    ).fetchall()
    return inserted + len(removed)


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


def prune_price_history(
    conn,
    cutoff: datetime,
    *,
    product_ids: list[str] | tuple[str, ...] | None = None,
) -> None:
    """Prune change events but retain one baseline event per product/subtype."""
    product_filter = ""
    product_params: list[str] = []
    if product_ids is not None:
        product_params = sorted({
            _tcg_product_id(value)
            for value in product_ids
            if _tcg_product_id(value)
        })
        if not product_params:
            return
        placeholders = ", ".join("?" for _ in product_params)
        product_filter = f"AND external_product_id IN ({placeholders})"

    params = (cutoff, *product_params, cutoff)
    conn.execute(
        f"""
        DELETE FROM catalog.provider_price_observations AS old
        WHERE old.game_id = 'mtg'
          AND old.provider_id = 'tcgcsv'
          AND old.captured_at < ?
          {product_filter}
          AND EXISTS (
              SELECT 1
              FROM catalog.provider_price_observations AS newer
              WHERE newer.game_id = old.game_id
                AND newer.provider_id = old.provider_id
                AND newer.external_product_id = old.external_product_id
                AND newer.subtype_name = old.subtype_name
                AND newer.captured_at < ?
                AND (
                    newer.captured_at > old.captured_at
                    OR (
                        newer.captured_at = old.captured_at
                        AND newer.source_updated > old.source_updated
                    )
                )
          )
        """,
        params,
    )

    conn.execute(
        f"""
        DELETE FROM catalog.product_price_history
        WHERE game_id = 'mtg'
          AND provider_id = 'tcgcsv'
          AND captured_at < ?
          {product_filter}
        """,
        (cutoff, *product_params),
    )
    conn.execute(
        f"""
        DELETE FROM app.price_alert_log
        WHERE game_id = 'mtg'
          AND provider_id = 'tcgcsv'
          AND alerted_at < ?
          {product_filter}
        """,
        (cutoff, *product_params),
    )
