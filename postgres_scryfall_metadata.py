"""PostgreSQL persistence for Scryfall printing metadata used by the app."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from psycopg.types.json import Jsonb


GAME_ID = "mtg"
PROVIDER_ID = "scryfall"
PRICE_PROVIDER_ID = "tcgcsv"


def _text(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def normalize_tcgplayer_id(value: Any) -> str | None:
    raw = _text(value)
    if raw is None:
        return None
    try:
        return str(int(float(raw)))
    except (TypeError, ValueError, OverflowError):
        return raw


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def fetch_owned_scryfall_ids(conn) -> list[str]:
    """Return distinct Scryfall printing IDs currently represented in MTG inventory."""
    rows = conn.execute(
        """
        SELECT DISTINCT btrim(i.scryfall_id) AS scryfall_id
        FROM app.inventory AS i
        WHERE i.game_id = %s
          AND i.scryfall_id IS NOT NULL
          AND btrim(i.scryfall_id) <> ''
        ORDER BY scryfall_id
        """,
        (GAME_ID,),
    ).fetchall()
    return [str(row["scryfall_id"]) for row in rows]


def fetch_missing_scryfall_ids(conn, limit: int = 500) -> list[str]:
    """Return owned MTG printings without cached Scryfall provider metadata."""
    safe_limit = max(1, min(1000, int(limit)))
    rows = conn.execute(
        """
        SELECT DISTINCT btrim(i.scryfall_id) AS scryfall_id
        FROM app.inventory AS i
        LEFT JOIN catalog.provider_printing_metadata AS metadata
          ON metadata.game_id = i.game_id
         AND metadata.provider_id = %s
         AND metadata.external_printing_id = btrim(i.scryfall_id)
        WHERE i.game_id = %s
          AND i.scryfall_id IS NOT NULL
          AND btrim(i.scryfall_id) <> ''
          AND metadata.external_printing_id IS NULL
        ORDER BY scryfall_id
        LIMIT %s
        """,
        (PROVIDER_ID, GAME_ID, safe_limit),
    ).fetchall()
    return [str(row["scryfall_id"]) for row in rows]


def save_provider_printing_metadata(
    conn,
    scryfall_id: str,
    data: dict[str, Any],
    *,
    updated_at: datetime | None = None,
) -> None:
    """Upsert Scryfall details and its TCGCSV product mapping in the caller's transaction.

    Provider metadata is retained even if the printing has not yet been resolved
    into the normalized catalog. The external Scryfall ID remains the stable key.
    """
    external_id = _text(scryfall_id)
    if not external_id:
        raise ValueError("A Scryfall printing ID is required.")
    if not isinstance(data, dict):
        raise TypeError("Scryfall metadata payload must be an object.")

    colors = data.get("colors") if isinstance(data.get("colors"), list) else []
    color_identity = (
        data.get("color_identity")
        if isinstance(data.get("color_identity"), list)
        else []
    )
    legalities = (
        data.get("legalities")
        if isinstance(data.get("legalities"), dict)
        else {}
    )
    colors_text = "".join(str(value).upper() for value in colors)
    timestamp = updated_at or datetime.now(timezone.utc)

    printing = conn.execute(
        """
        SELECT printing_id
        FROM catalog.external_ids
        WHERE game_id = %s
          AND provider_id = %s
          AND entity_type = 'printing'
          AND external_id_type = 'scryfall_id'
          AND external_id = %s
        LIMIT 1
        """,
        (GAME_ID, PROVIDER_ID, external_id),
    ).fetchone()
    printing_id = printing["printing_id"] if printing else None

    conn.execute(
        """
        INSERT INTO catalog.provider_printing_metadata (
            game_id, provider_id, external_printing_id, printing_id,
            name, type_line, colors, colors_text, color_identity,
            mana_value, mana_cost, oracle_text, external_oracle_id,
            legalities, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (game_id, provider_id, external_printing_id)
        DO UPDATE SET
            printing_id = COALESCE(EXCLUDED.printing_id,
                                   catalog.provider_printing_metadata.printing_id),
            name = EXCLUDED.name,
            type_line = EXCLUDED.type_line,
            colors = EXCLUDED.colors,
            colors_text = EXCLUDED.colors_text,
            color_identity = EXCLUDED.color_identity,
            mana_value = EXCLUDED.mana_value,
            mana_cost = EXCLUDED.mana_cost,
            oracle_text = EXCLUDED.oracle_text,
            external_oracle_id = EXCLUDED.external_oracle_id,
            legalities = EXCLUDED.legalities,
            updated_at = EXCLUDED.updated_at
        """,
        (
            GAME_ID,
            PROVIDER_ID,
            external_id,
            printing_id,
            _text(data.get("name")),
            _text(data.get("type_line")),
            Jsonb(colors),
            colors_text,
            Jsonb(color_identity),
            _number(data.get("mana_value")),
            _text(data.get("mana_cost")),
            _text(data.get("oracle_text")),
            _text(data.get("oracle_id")),
            Jsonb(legalities),
            timestamp,
        ),
    )

    finishes = data.get("finishes") if isinstance(data.get("finishes"), list) else []
    tcgplayer_id = normalize_tcgplayer_id(data.get("tcgplayer_id"))
    conn.execute(
        """
        INSERT INTO catalog.printing_product_mappings (
            game_id, source_provider_id, source_external_printing_id,
            target_provider_id, printing_id, target_external_product_id,
            finishes, card_name, set_code, collector_number, resolved_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (
            game_id, source_provider_id, source_external_printing_id,
            target_provider_id
        )
        DO UPDATE SET
            printing_id = COALESCE(
                EXCLUDED.printing_id,
                catalog.printing_product_mappings.printing_id
            ),
            target_external_product_id = EXCLUDED.target_external_product_id,
            finishes = EXCLUDED.finishes,
            card_name = EXCLUDED.card_name,
            set_code = EXCLUDED.set_code,
            collector_number = EXCLUDED.collector_number,
            resolved_at = EXCLUDED.resolved_at
        """,
        (
            GAME_ID,
            PROVIDER_ID,
            external_id,
            PRICE_PROVIDER_ID,
            printing_id,
            tcgplayer_id,
            Jsonb(finishes),
            _text(data.get("name")),
            _text(data.get("set")),
            _text(data.get("collector_number")),
            timestamp,
        ),
    )

    printed_name = _text(data.get("printed_name"))
    canonical_name = _text(data.get("name"))
    if printed_name and canonical_name and printed_name.casefold() != canonical_name.casefold():
        conn.execute(
            """
            INSERT INTO app.card_aliases (
                game_id, provider_id, external_printing_id, printing_id, alias
            )
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (GAME_ID, PROVIDER_ID, external_id, printing_id, printed_name),
        )
