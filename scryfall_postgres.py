"""PostgreSQL-only Scryfall catalog importer.

The caller owns the connection and outer transaction. Import operations use
nested transactions (savepoints) so a failed import cannot leave partial data
in the caller's transaction.

This module does not open or modify either production SQLite database.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

# Reuse the established streaming gzip/JSONL and legacy-array reader.
# Importing the module does not open its SQLite database.
from scryfall_updater import (
    IMAGE_WORKERS,
    _image_for_card,
    hash_image,
    iter_bulk_records,
)


GAME_ID = "mtg"
PROVIDER_ID = "scryfall"
SCRYFALL_IMPORT_LOCK = 731942810553


def _source_id(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _source_date(value: Any) -> date | None:
    text = _source_id(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise RuntimeError(f"Invalid Scryfall date: {text!r}") from exc


def _source_timestamp(value: str | None) -> datetime:
    text = _source_id(value)
    if not text:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(f"Invalid Scryfall build timestamp: {text!r}") from exc


def _external_target(
    conn,
    external_id_type: str,
    external_id: str,
    entity_type: str,
) -> int | None:
    target_column = {
        "card": "card_id",
        "printing": "printing_id",
        "set": "set_id",
        "illustration": "illustration_id",
    }.get(entity_type)
    if not target_column:
        raise ValueError(f"Unsupported Scryfall entity type: {entity_type}")

    row = conn.execute(
        f"""
        SELECT {target_column} AS target_id
        FROM catalog.external_ids
        WHERE game_id = %s
          AND provider_id = %s
          AND entity_type = %s
          AND external_id_type = %s
          AND external_id = %s
        """,
        (GAME_ID, PROVIDER_ID, entity_type, external_id_type, external_id),
    ).fetchone()
    return int(row["target_id"]) if row and row["target_id"] is not None else None


def _record_external_id(
    conn,
    external_id_type: str,
    external_id: str,
    entity_type: str,
    target_id: int,
) -> None:
    target_column = {
        "card": "card_id",
        "printing": "printing_id",
        "set": "set_id",
        "illustration": "illustration_id",
    }[entity_type]

    conn.execute(
        f"""
        INSERT INTO catalog.external_ids (
            game_id, provider_id, entity_type, external_id_type,
            external_id, {target_column}
        )
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (game_id, provider_id, external_id_type, external_id)
        DO NOTHING
        """,
        (
            GAME_ID, PROVIDER_ID, entity_type,
            external_id_type, external_id, target_id,
        ),
    )


def _ensure_set(conn, record: dict[str, Any], cache: dict[str, int]) -> int | None:
    external_id = _source_id(record.get("set_id"))
    if not external_id:
        return None
    if external_id in cache:
        return cache[external_id]

    name = _source_id(record.get("set_name")) or _source_id(record.get("set"))
    if not name:
        return None

    set_id = _external_target(conn, "set_id", external_id, "set")
    if set_id is None:
        set_id = conn.execute(
            """
            INSERT INTO catalog.sets (
                game_id, name, set_code, set_type, released_at, metadata
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING set_id
            """,
            (
                GAME_ID,
                name,
                _source_id(record.get("set")),
                _source_id(record.get("set_type")),
                _source_date(record.get("released_at")),
                Jsonb({"source": "scryfall", "external_set_id": external_id}),
            ),
        ).fetchone()["set_id"]
        _record_external_id(conn, "set_id", external_id, "set", set_id)
    else:
        conn.execute(
            """
            UPDATE catalog.sets
            SET name = %s,
                set_code = %s,
                set_type = %s,
                released_at = %s,
                updated_at = now()
            WHERE set_id = %s AND game_id = %s
            """,
            (
                name,
                _source_id(record.get("set")),
                _source_id(record.get("set_type")),
                _source_date(record.get("released_at")),
                set_id,
                GAME_ID,
            ),
        )

    cache[external_id] = int(set_id)
    return int(set_id)


def _ensure_card(
    conn,
    oracle_id: str | None,
    record: dict[str, Any],
    cache: dict[str, int],
) -> int | None:
    if not oracle_id:
        return None
    if oracle_id in cache:
        return cache[oracle_id]

    name = _source_id(record.get("name")) or oracle_id
    card_id = _external_target(conn, "oracle_id", oracle_id, "card")

    if card_id is None:
        card_id = conn.execute(
            """
            INSERT INTO catalog.cards (
                game_id, name, type_line, rules_text, metadata
            )
            VALUES (%s, %s, %s, %s, %s)
            RETURNING card_id
            """,
            (
                GAME_ID,
                name,
                _source_id(record.get("type_line")),
                _source_id(record.get("oracle_text")),
                Jsonb({"source": "scryfall", "oracle_id": oracle_id}),
            ),
        ).fetchone()["card_id"]
        _record_external_id(conn, "oracle_id", oracle_id, "card", card_id)
    else:
        conn.execute(
            """
            UPDATE catalog.cards
            SET name = %s,
                type_line = %s,
                rules_text = %s,
                updated_at = now()
            WHERE card_id = %s AND game_id = %s
            """,
            (
                name,
                _source_id(record.get("type_line")),
                _source_id(record.get("oracle_text")),
                card_id,
                GAME_ID,
            ),
        )

    cache[oracle_id] = int(card_id)
    return int(card_id)


def _ensure_illustration(
    conn,
    external_id: str | None,
    cache: dict[str, int],
) -> int | None:
    if not external_id:
        return None
    if external_id in cache:
        return cache[external_id]

    illustration_id = _external_target(
        conn, "illustration_id", external_id, "illustration"
    )
    if illustration_id is None:
        illustration_id = conn.execute(
            """
            INSERT INTO catalog.illustrations (game_id, metadata)
            VALUES (%s, %s)
            RETURNING illustration_id
            """,
            (
                GAME_ID,
                Jsonb({
                    "source": "scryfall",
                    "external_illustration_id": external_id,
                }),
            ),
        ).fetchone()["illustration_id"]
        _record_external_id(
            conn, "illustration_id", external_id, "illustration",
            illustration_id,
        )

    cache[external_id] = int(illustration_id)
    return int(illustration_id)


def _ensure_image(
    conn,
    image_uri: str | None,
    cache: dict[str, int],
) -> int | None:
    if not image_uri:
        return None
    if image_uri in cache:
        return cache[image_uri]

    row = conn.execute(
        """
        INSERT INTO catalog.images (
            game_id, provider_id, image_uri, image_role, metadata
        )
        VALUES (%s, %s, %s, 'card_face', %s)
        ON CONFLICT (game_id, provider_id, image_uri) DO NOTHING
        RETURNING image_id
        """,
        (
            GAME_ID,
            PROVIDER_ID,
            image_uri,
            Jsonb({"source": "scryfall"}),
        ),
    ).fetchone()

    if row is None:
        row = conn.execute(
            """
            SELECT image_id
            FROM catalog.images
            WHERE game_id = %s AND provider_id = %s AND image_uri = %s
            """,
            (GAME_ID, PROVIDER_ID, image_uri),
        ).fetchone()

    image_id = int(row["image_id"])
    cache[image_uri] = image_id
    return image_id


def _face_records(record: dict[str, Any]) -> list[dict[str, Any]]:
    faces = record.get("card_faces")
    if isinstance(faces, list) and faces:
        return [face for face in faces if isinstance(face, dict)]
    return [record]


def _face_image_uri(face: dict[str, Any]) -> str | None:
    image_uris = face.get("image_uris")
    if isinstance(image_uris, dict):
        return _source_id(image_uris.get("normal"))
    return None


def _import_one_printing(
    conn,
    record: dict[str, Any],
    build_timestamp: datetime,
    caches: dict[str, dict[str, int]],
) -> bool:
    external_printing_id = _source_id(record.get("id"))
    if not external_printing_id:
        return False

    faces = _face_records(record)
    oracle_id = _source_id(record.get("oracle_id"))
    if not oracle_id:
        oracle_id = next(
            (_source_id(face.get("oracle_id")) for face in faces
             if _source_id(face.get("oracle_id"))),
            None,
        )

    set_id = _ensure_set(conn, record, caches["sets"])
    card_id = _ensure_card(conn, oracle_id, record, caches["cards"])

    top_image_uri = _image_for_card(record)
    top_image_id = _ensure_image(conn, top_image_uri, caches["images"])

    printing_id = _external_target(
        conn, "scryfall_id", external_printing_id, "printing"
    )
    finishes = record.get("finishes")
    if not isinstance(finishes, list):
        finishes = []

    if printing_id is None:
        printing_id = conn.execute(
            """
            INSERT INTO catalog.printings (
                game_id, card_id, set_id, collector_number, language_code,
                rarity, available_finishes, image_id, released_at, is_promo,
                metadata
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING printing_id
            """,
            (
                GAME_ID,
                card_id,
                set_id,
                _source_id(record.get("collector_number")),
                _source_id(record.get("lang")),
                _source_id(record.get("rarity")),
                [str(value) for value in finishes if value is not None],
                top_image_id,
                _source_date(record.get("released_at")),
                record.get("promo") if isinstance(record.get("promo"), bool) else None,
                Jsonb(record),
            ),
        ).fetchone()["printing_id"]
        _record_external_id(
            conn, "scryfall_id", external_printing_id, "printing", printing_id
        )
    else:
        conn.execute(
            """
            UPDATE catalog.printings
            SET card_id = %s,
                set_id = %s,
                collector_number = %s,
                language_code = %s,
                rarity = %s,
                available_finishes = %s,
                image_id = %s,
                released_at = %s,
                is_promo = %s,
                metadata = %s,
                updated_at = now()
            WHERE printing_id = %s AND game_id = %s
            """,
            (
                card_id,
                set_id,
                _source_id(record.get("collector_number")),
                _source_id(record.get("lang")),
                _source_id(record.get("rarity")),
                [str(value) for value in finishes if value is not None],
                top_image_id,
                _source_date(record.get("released_at")),
                record.get("promo") if isinstance(record.get("promo"), bool) else None,
                Jsonb(record),
                printing_id,
                GAME_ID,
            ),
        )

    face_rows = []
    for face_index, face in enumerate(faces):
        face_oracle_id = _source_id(face.get("oracle_id")) or oracle_id
        illustration_external_id = (
            _source_id(face.get("illustration_id"))
            or (
                _source_id(record.get("illustration_id"))
                if face_index == 0 else None
            )
        )
        illustration_id = _ensure_illustration(
            conn, illustration_external_id, caches["illustrations"]
        )
        image_uri = _face_image_uri(face)
        if not image_uri and len(faces) == 1:
            image_uri = top_image_uri
        image_id = _ensure_image(conn, image_uri, caches["images"])

        face_rows.append((
            printing_id,
            GAME_ID,
            face_index,
            _source_id(face.get("name")),
            _source_id(face.get("type_line")),
            _source_id(face.get("oracle_text")),
            _source_id(face.get("flavor_text")),
            image_id,
            illustration_id,
            Jsonb(face),
        ))

    conn.execute(
        """
        DELETE FROM catalog.printing_faces
        WHERE printing_id = %s AND game_id = %s
        """,
        (printing_id, GAME_ID),
    )
    if face_rows:
        with conn.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO catalog.printing_faces (
                    printing_id, game_id, face_index, name, type_line,
                    rules_text, flavor_text, image_id, illustration_id, metadata
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                face_rows,
            )

    colors = record.get("colors")
    if not isinstance(colors, list):
        colors = None
    color_identity = record.get("color_identity")
    if not isinstance(color_identity, list):
        color_identity = None
    legalities = record.get("legalities")
    if not isinstance(legalities, dict):
        legalities = None

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
            printing_id = EXCLUDED.printing_id,
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
            external_printing_id,
            printing_id,
            _source_id(record.get("name")),
            _source_id(record.get("type_line")),
            Jsonb(colors) if colors is not None else None,
            "".join(str(value) for value in colors) if colors is not None else None,
            Jsonb(color_identity) if color_identity is not None else None,
            record.get("mana_value"),
            _source_id(record.get("mana_cost")),
            _source_id(record.get("oracle_text")),
            oracle_id,
            Jsonb(legalities) if legalities is not None else None,
            build_timestamp,
        ),
    )
    return top_image_uri is not None



def _create_printing_staging(conn) -> None:
    names = (
        "_scryfall_printing_raw",
        "_scryfall_printing_seen",
        "_scryfall_printing_stage",
        "_scryfall_face_stage",
        "_scryfall_set_stage",
        "_scryfall_card_stage",
        "_scryfall_illustration_stage",
        "_scryfall_image_stage",
    )
    for name in names:
        conn.execute(f"DROP TABLE IF EXISTS {name}")

    conn.execute("""
        CREATE TEMP TABLE _scryfall_printing_raw (
            stage_row BIGINT GENERATED ALWAYS AS IDENTITY,
            payload JSONB NOT NULL
        ) ON COMMIT DROP
    """)
    conn.execute("""
        CREATE TEMP TABLE _scryfall_printing_seen (
            external_printing_id TEXT PRIMARY KEY
        ) ON COMMIT DROP
    """)
    conn.execute("""
        CREATE TEMP TABLE _scryfall_printing_stage (
            stage_row BIGINT PRIMARY KEY,
            payload JSONB NOT NULL,
            external_printing_id TEXT NOT NULL,
            external_oracle_id TEXT,
            external_set_id TEXT,
            top_image_uri TEXT
        ) ON COMMIT DROP
    """)
    conn.execute("""
        CREATE TEMP TABLE _scryfall_face_stage (
            external_printing_id TEXT NOT NULL,
            face_index INTEGER NOT NULL,
            name TEXT,
            type_line TEXT,
            rules_text TEXT,
            flavor_text TEXT,
            image_uri TEXT,
            external_illustration_id TEXT,
            metadata JSONB NOT NULL,
            PRIMARY KEY (external_printing_id, face_index)
        ) ON COMMIT DROP
    """)
    conn.execute("""
        CREATE TEMP TABLE _scryfall_set_stage (
            external_set_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            set_code TEXT,
            set_type TEXT,
            released_at DATE
        ) ON COMMIT DROP
    """)
    conn.execute("""
        CREATE TEMP TABLE _scryfall_card_stage (
            external_oracle_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            type_line TEXT,
            rules_text TEXT
        ) ON COMMIT DROP
    """)
    conn.execute("""
        CREATE TEMP TABLE _scryfall_illustration_stage (
            external_illustration_id TEXT PRIMARY KEY
        ) ON COMMIT DROP
    """)
    conn.execute("""
        CREATE TEMP TABLE _scryfall_image_stage (
            image_uri TEXT PRIMARY KEY
        ) ON COMMIT DROP
    """)


def _prepare_printing_batch(conn) -> None:
    conn.execute("""
        TRUNCATE
            _scryfall_printing_stage,
            _scryfall_face_stage,
            _scryfall_set_stage,
            _scryfall_card_stage,
            _scryfall_illustration_stage,
            _scryfall_image_stage
    """)

    duplicate = conn.execute("""
        SELECT payload->>'id' AS external_id
        FROM _scryfall_printing_raw
        GROUP BY payload->>'id'
        HAVING COUNT(*) > 1
        LIMIT 1
    """).fetchone()
    if duplicate:
        raise RuntimeError(
            "Scryfall printing batch contains duplicate IDs: "
            f"{duplicate['external_id']}"
        )

    conn.execute("""
        INSERT INTO _scryfall_printing_stage (
            stage_row, payload, external_printing_id,
            external_oracle_id, external_set_id, top_image_uri
        )
        SELECT
            raw.stage_row,
            raw.payload,
            NULLIF(btrim(raw.payload->>'id'), ''),
            COALESCE(
                NULLIF(btrim(raw.payload->>'oracle_id'), ''),
                (
                    SELECT NULLIF(btrim(face.value->>'oracle_id'), '')
                    FROM jsonb_array_elements(
                        CASE
                            WHEN jsonb_typeof(raw.payload->'card_faces') = 'array'
                            THEN CASE
                                WHEN jsonb_array_length(raw.payload->'card_faces') > 0
                                THEN raw.payload->'card_faces'
                                ELSE jsonb_build_array(raw.payload)
                            END
                            ELSE jsonb_build_array(raw.payload)
                        END
                    ) WITH ORDINALITY AS face(value, ordinality)
                    WHERE face.ordinality = 1
                    LIMIT 1
                )
            ),
            NULLIF(btrim(raw.payload->>'set_id'), ''),
            COALESCE(
                NULLIF(raw.payload->'image_uris'->>'normal', ''),
                (
                    SELECT NULLIF(face.value->'image_uris'->>'normal', '')
                    FROM jsonb_array_elements(
                        CASE
                            WHEN jsonb_typeof(raw.payload->'card_faces') = 'array'
                            THEN CASE
                                WHEN jsonb_array_length(raw.payload->'card_faces') > 0
                                THEN raw.payload->'card_faces'
                                ELSE jsonb_build_array(raw.payload)
                            END
                            ELSE jsonb_build_array(raw.payload)
                        END
                    ) WITH ORDINALITY AS face(value, ordinality)
                    ORDER BY face.ordinality
                    LIMIT 1
                )
            )
        FROM _scryfall_printing_raw AS raw
    """)

    conn.execute("""
        WITH expanded AS (
            SELECT
                printing.external_printing_id,
                (face.ordinality - 1)::integer AS face_index,
                NULLIF(face.value->>'name', '') AS name,
                NULLIF(face.value->>'type_line', '') AS type_line,
                NULLIF(face.value->>'oracle_text', '') AS rules_text,
                NULLIF(face.value->>'flavor_text', '') AS flavor_text,
                NULLIF(
                    face.value->'image_uris'->>'normal', ''
                ) AS image_uri,
                printing.top_image_uri,
                COUNT(*) OVER (
                    PARTITION BY printing.external_printing_id
                ) AS face_count,
                COALESCE(
                    NULLIF(face.value->>'illustration_id', ''),
                    CASE
                        WHEN face.ordinality = 1
                        THEN NULLIF(printing.payload->>'illustration_id', '')
                    END
                ) AS external_illustration_id,
                face.value AS metadata
            FROM _scryfall_printing_stage AS printing
            CROSS JOIN LATERAL jsonb_array_elements(
                CASE
                    WHEN jsonb_typeof(printing.payload->'card_faces') = 'array'
                    THEN CASE
                        WHEN jsonb_array_length(printing.payload->'card_faces') > 0
                        THEN printing.payload->'card_faces'
                        ELSE jsonb_build_array(printing.payload)
                    END
                    ELSE jsonb_build_array(printing.payload)
                END
            ) WITH ORDINALITY AS face(value, ordinality)
        )
        INSERT INTO _scryfall_face_stage (
            external_printing_id, face_index, name, type_line,
            rules_text, flavor_text, image_uri,
            external_illustration_id, metadata
        )
        SELECT
            external_printing_id,
            face_index,
            name,
            type_line,
            rules_text,
            flavor_text,
            COALESCE(
                image_uri,
                CASE WHEN face_count = 1 THEN top_image_uri END
            ),
            external_illustration_id,
            metadata
        FROM expanded
    """)

    conn.execute("""
        INSERT INTO _scryfall_set_stage (
            external_set_id, name, set_code, set_type, released_at
        )
        SELECT DISTINCT ON (external_set_id)
            external_set_id,
            COALESCE(
                NULLIF(payload->>'set_name', ''),
                NULLIF(payload->>'set', ''),
                external_set_id
            ),
            NULLIF(payload->>'set', ''),
            NULLIF(payload->>'set_type', ''),
            NULLIF(payload->>'released_at', '')::date
        FROM _scryfall_printing_stage
        WHERE external_set_id IS NOT NULL
        ORDER BY external_set_id, stage_row DESC
    """)

    conn.execute("""
        INSERT INTO _scryfall_card_stage (
            external_oracle_id, name, type_line, rules_text
        )
        SELECT DISTINCT ON (external_oracle_id)
            external_oracle_id,
            COALESCE(NULLIF(payload->>'name', ''), external_oracle_id),
            NULLIF(payload->>'type_line', ''),
            NULLIF(payload->>'oracle_text', '')
        FROM _scryfall_printing_stage
        WHERE external_oracle_id IS NOT NULL
        ORDER BY external_oracle_id, stage_row DESC
    """)

    conn.execute("""
        INSERT INTO _scryfall_illustration_stage (external_illustration_id)
        SELECT DISTINCT external_illustration_id
        FROM _scryfall_face_stage
        WHERE external_illustration_id IS NOT NULL
    """)

    conn.execute("""
        INSERT INTO _scryfall_image_stage (image_uri)
        SELECT top_image_uri
        FROM _scryfall_printing_stage
        WHERE top_image_uri IS NOT NULL
        UNION
        SELECT image_uri
        FROM _scryfall_face_stage
        WHERE image_uri IS NOT NULL
    """)


def _merge_printing_batch(conn, build_timestamp: datetime) -> None:
    # An external ID must never point to an entity of the wrong type.
    wrong_mapping = conn.execute("""
        SELECT e.external_id_type, e.external_id, e.entity_type
        FROM catalog.external_ids AS e
        WHERE e.game_id = %s
          AND e.provider_id = %s
          AND (
              (e.external_id_type = 'set_id' AND e.entity_type <> 'set')
              OR (e.external_id_type = 'oracle_id' AND e.entity_type <> 'card')
              OR (e.external_id_type = 'scryfall_id' AND e.entity_type <> 'printing')
              OR (e.external_id_type = 'illustration_id'
                  AND e.entity_type <> 'illustration')
          )
          AND (
              (e.external_id_type = 'set_id' AND e.external_id IN (
                  SELECT external_set_id FROM _scryfall_set_stage
              ))
              OR
              (e.external_id_type = 'oracle_id' AND e.external_id IN (
                  SELECT external_oracle_id FROM _scryfall_card_stage
              ))
              OR
              (e.external_id_type = 'scryfall_id' AND e.external_id IN (
                  SELECT external_printing_id FROM _scryfall_printing_stage
              ))
              OR
              (e.external_id_type = 'illustration_id' AND e.external_id IN (
                  SELECT external_illustration_id
                  FROM _scryfall_illustration_stage
              ))
          )
        LIMIT 1
    """, (GAME_ID, PROVIDER_ID)).fetchone()

    if wrong_mapping:
        raise RuntimeError(
            "Scryfall external identifier has an incompatible existing mapping: "
            f"{wrong_mapping['external_id_type']}={wrong_mapping['external_id']}"
        )

    # Sets: insert missing identities, then refresh their source metadata.
    conn.execute("""
        WITH inserted AS (
            INSERT INTO catalog.sets (
                game_id, name, set_code, set_type, released_at, metadata
            )
            SELECT
                %s, s.name, s.set_code, s.set_type, s.released_at,
                jsonb_build_object(
                    'source', 'scryfall',
                    'external_set_id', s.external_set_id
                )
            FROM _scryfall_set_stage AS s
            WHERE NOT EXISTS (
                SELECT 1
                FROM catalog.external_ids AS e
                WHERE e.game_id = %s
                  AND e.provider_id = %s
                  AND e.external_id_type = 'set_id'
                  AND e.external_id = s.external_set_id
            )
            RETURNING set_id, metadata->>'external_set_id' AS external_id
        )
        INSERT INTO catalog.external_ids (
            game_id, provider_id, entity_type,
            external_id_type, external_id, set_id
        )
        SELECT %s, %s, 'set', 'set_id', external_id, set_id
        FROM inserted
        ON CONFLICT (game_id, provider_id, external_id_type, external_id)
        DO NOTHING
    """, (GAME_ID, GAME_ID, PROVIDER_ID, GAME_ID, PROVIDER_ID))

    conn.execute("""
        UPDATE catalog.sets AS target
        SET name = source.name,
            set_code = source.set_code,
            set_type = source.set_type,
            released_at = source.released_at,
            updated_at = now()
        FROM _scryfall_set_stage AS source
        JOIN catalog.external_ids AS e
          ON e.game_id = %s
         AND e.provider_id = %s
         AND e.entity_type = 'set'
         AND e.external_id_type = 'set_id'
         AND e.external_id = source.external_set_id
        WHERE target.set_id = e.set_id
          AND target.game_id = %s
    """, (GAME_ID, PROVIDER_ID, GAME_ID))

    # Canonical cards: Oracle IDs are the identity, not the printing name.
    conn.execute("""
        WITH inserted AS (
            INSERT INTO catalog.cards (
                game_id, name, type_line, rules_text, metadata
            )
            SELECT
                %s, c.name, c.type_line, c.rules_text,
                jsonb_build_object(
                    'source', 'scryfall',
                    'oracle_id', c.external_oracle_id
                )
            FROM _scryfall_card_stage AS c
            WHERE NOT EXISTS (
                SELECT 1
                FROM catalog.external_ids AS e
                WHERE e.game_id = %s
                  AND e.provider_id = %s
                  AND e.external_id_type = 'oracle_id'
                  AND e.external_id = c.external_oracle_id
            )
            RETURNING card_id, metadata->>'oracle_id' AS external_id
        )
        INSERT INTO catalog.external_ids (
            game_id, provider_id, entity_type,
            external_id_type, external_id, card_id
        )
        SELECT %s, %s, 'card', 'oracle_id', external_id, card_id
        FROM inserted
        ON CONFLICT (game_id, provider_id, external_id_type, external_id)
        DO NOTHING
    """, (GAME_ID, GAME_ID, PROVIDER_ID, GAME_ID, PROVIDER_ID))

    conn.execute("""
        UPDATE catalog.cards AS target
        SET name = source.name,
            type_line = source.type_line,
            rules_text = source.rules_text,
            updated_at = now()
        FROM _scryfall_card_stage AS source
        JOIN catalog.external_ids AS e
          ON e.game_id = %s
         AND e.provider_id = %s
         AND e.entity_type = 'card'
         AND e.external_id_type = 'oracle_id'
         AND e.external_id = source.external_oracle_id
        WHERE target.card_id = e.card_id
          AND target.game_id = %s
    """, (GAME_ID, PROVIDER_ID, GAME_ID))

    # Every source illustration identity is retained, even before a face
    # references it or when the source export supplies only a Tagger reference.
    conn.execute("""
        WITH inserted AS (
            INSERT INTO catalog.illustrations (game_id, metadata)
            SELECT
                %s,
                jsonb_build_object(
                    'source', 'scryfall',
                    'external_illustration_id', i.external_illustration_id
                )
            FROM _scryfall_illustration_stage AS i
            WHERE NOT EXISTS (
                SELECT 1
                FROM catalog.external_ids AS e
                WHERE e.game_id = %s
                  AND e.provider_id = %s
                  AND e.external_id_type = 'illustration_id'
                  AND e.external_id = i.external_illustration_id
            )
            RETURNING
                illustration_id,
                metadata->>'external_illustration_id' AS external_id
        )
        INSERT INTO catalog.external_ids (
            game_id, provider_id, entity_type,
            external_id_type, external_id, illustration_id
        )
        SELECT
            %s, %s, 'illustration', 'illustration_id',
            external_id, illustration_id
        FROM inserted
        ON CONFLICT (game_id, provider_id, external_id_type, external_id)
        DO NOTHING
    """, (GAME_ID, GAME_ID, PROVIDER_ID, GAME_ID, PROVIDER_ID))

    # Images are immutable URI resources. A changed URL creates a new image,
    # leaving hashes for the prior URI attached only to the prior resource.
    conn.execute("""
        INSERT INTO catalog.images (
            game_id, provider_id, image_uri, image_role, metadata
        )
        SELECT
            %s, %s, image_uri, 'card_face',
            jsonb_build_object('source', 'scryfall')
        FROM _scryfall_image_stage
        ON CONFLICT (game_id, provider_id, image_uri) DO NOTHING
    """, (GAME_ID, PROVIDER_ID))

    # Create printings whose Scryfall source identity is not yet registered.
    conn.execute("""
        WITH inserted AS (
            INSERT INTO catalog.printings (
                game_id, card_id, set_id, collector_number, language_code,
                rarity, available_finishes, image_id, released_at, is_promo,
                metadata
            )
            SELECT
                %s,
                card_external.card_id,
                set_external.set_id,
                NULLIF(source.payload->>'collector_number', ''),
                NULLIF(source.payload->>'lang', ''),
                NULLIF(source.payload->>'rarity', ''),
                CASE
                    WHEN jsonb_typeof(source.payload->'finishes') = 'array'
                    THEN ARRAY(
                        SELECT jsonb_array_elements_text(source.payload->'finishes')
                    )
                    ELSE ARRAY[]::text[]
                END,
                image.image_id,
                NULLIF(source.payload->>'released_at', '')::date,
                CASE
                    WHEN jsonb_typeof(source.payload->'promo') = 'boolean'
                    THEN (source.payload->>'promo')::boolean
                    ELSE NULL
                END,
                source.payload
            FROM _scryfall_printing_stage AS source
            LEFT JOIN catalog.external_ids AS card_external
              ON card_external.game_id = %s
             AND card_external.provider_id = %s
             AND card_external.entity_type = 'card'
             AND card_external.external_id_type = 'oracle_id'
             AND card_external.external_id = source.external_oracle_id
            LEFT JOIN catalog.external_ids AS set_external
              ON set_external.game_id = %s
             AND set_external.provider_id = %s
             AND set_external.entity_type = 'set'
             AND set_external.external_id_type = 'set_id'
             AND set_external.external_id = source.external_set_id
            LEFT JOIN catalog.images AS image
              ON image.game_id = %s
             AND image.provider_id = %s
             AND image.image_uri = source.top_image_uri
            WHERE NOT EXISTS (
                SELECT 1
                FROM catalog.external_ids AS existing
                WHERE existing.game_id = %s
                  AND existing.provider_id = %s
                  AND existing.entity_type = 'printing'
                  AND existing.external_id_type = 'scryfall_id'
                  AND existing.external_id = source.external_printing_id
            )
            RETURNING printing_id, metadata->>'id' AS external_id
        )
        INSERT INTO catalog.external_ids (
            game_id, provider_id, entity_type,
            external_id_type, external_id, printing_id
        )
        SELECT %s, %s, 'printing', 'scryfall_id', external_id, printing_id
        FROM inserted
        ON CONFLICT (game_id, provider_id, external_id_type, external_id)
        DO NOTHING
    """, (
        GAME_ID,
        GAME_ID, PROVIDER_ID,
        GAME_ID, PROVIDER_ID,
        GAME_ID, PROVIDER_ID,
        GAME_ID, PROVIDER_ID,
        GAME_ID, PROVIDER_ID,
    ))

    # Refresh existing and newly-created printing records using the stable
    # Scryfall ID mapping. Collector numbers are deliberately not unique keys.
    conn.execute("""
        UPDATE catalog.printings AS target
        SET card_id = card_external.card_id,
            set_id = set_external.set_id,
            collector_number = NULLIF(source.payload->>'collector_number', ''),
            language_code = NULLIF(source.payload->>'lang', ''),
            rarity = NULLIF(source.payload->>'rarity', ''),
            available_finishes = CASE
                WHEN jsonb_typeof(source.payload->'finishes') = 'array'
                THEN ARRAY(
                    SELECT jsonb_array_elements_text(source.payload->'finishes')
                )
                ELSE ARRAY[]::text[]
            END,
            image_id = image.image_id,
            released_at = NULLIF(source.payload->>'released_at', '')::date,
            is_promo = CASE
                WHEN jsonb_typeof(source.payload->'promo') = 'boolean'
                THEN (source.payload->>'promo')::boolean
                ELSE NULL
            END,
            metadata = source.payload,
            updated_at = now()
        FROM _scryfall_printing_stage AS source
        JOIN catalog.external_ids AS printing_external
          ON printing_external.game_id = %s
         AND printing_external.provider_id = %s
         AND printing_external.entity_type = 'printing'
         AND printing_external.external_id_type = 'scryfall_id'
         AND printing_external.external_id = source.external_printing_id
        LEFT JOIN catalog.external_ids AS card_external
          ON card_external.game_id = %s
         AND card_external.provider_id = %s
         AND card_external.entity_type = 'card'
         AND card_external.external_id_type = 'oracle_id'
         AND card_external.external_id = source.external_oracle_id
        LEFT JOIN catalog.external_ids AS set_external
          ON set_external.game_id = %s
         AND set_external.provider_id = %s
         AND set_external.entity_type = 'set'
         AND set_external.external_id_type = 'set_id'
         AND set_external.external_id = source.external_set_id
        LEFT JOIN catalog.images AS image
          ON image.game_id = %s
         AND image.provider_id = %s
         AND image.image_uri = source.top_image_uri
        WHERE target.printing_id = printing_external.printing_id
          AND target.game_id = %s
    """, (
        GAME_ID, PROVIDER_ID,
        GAME_ID, PROVIDER_ID,
        GAME_ID, PROVIDER_ID,
        GAME_ID, PROVIDER_ID,
        GAME_ID,
    ))

    # Face records are replaced for this printing batch only.
    conn.execute("""
        DELETE FROM catalog.printing_faces AS face
        USING catalog.external_ids AS external
        JOIN _scryfall_printing_stage AS source
          ON source.external_printing_id = external.external_id
        WHERE external.game_id = %s
          AND external.provider_id = %s
          AND external.entity_type = 'printing'
          AND external.external_id_type = 'scryfall_id'
          AND face.printing_id = external.printing_id
          AND face.game_id = %s
    """, (GAME_ID, PROVIDER_ID, GAME_ID))

    conn.execute("""
        INSERT INTO catalog.printing_faces (
            printing_id, game_id, face_index, name, type_line,
            rules_text, flavor_text, image_id, illustration_id, metadata
        )
        SELECT
            printing_external.printing_id,
            %s,
            face.face_index,
            face.name,
            face.type_line,
            face.rules_text,
            face.flavor_text,
            image.image_id,
            illustration_external.illustration_id,
            face.metadata
        FROM _scryfall_face_stage AS face
        JOIN catalog.external_ids AS printing_external
          ON printing_external.game_id = %s
         AND printing_external.provider_id = %s
         AND printing_external.entity_type = 'printing'
         AND printing_external.external_id_type = 'scryfall_id'
         AND printing_external.external_id = face.external_printing_id
        LEFT JOIN catalog.images AS image
          ON image.game_id = %s
         AND image.provider_id = %s
         AND image.image_uri = face.image_uri
        LEFT JOIN catalog.external_ids AS illustration_external
          ON illustration_external.game_id = %s
         AND illustration_external.provider_id = %s
         AND illustration_external.entity_type = 'illustration'
         AND illustration_external.external_id_type = 'illustration_id'
         AND illustration_external.external_id = face.external_illustration_id
    """, (
        GAME_ID,
        GAME_ID, PROVIDER_ID,
        GAME_ID, PROVIDER_ID,
        GAME_ID, PROVIDER_ID,
    ))

    # Provider metadata retains the complete Scryfall-specific fields without
    # forcing them into the game-neutral canonical card record.
    conn.execute("""
        INSERT INTO catalog.provider_printing_metadata (
            game_id, provider_id, external_printing_id, printing_id,
            name, type_line, colors, colors_text, color_identity,
            mana_value, mana_cost, oracle_text, external_oracle_id,
            legalities, updated_at
        )
        SELECT
            %s,
            %s,
            source.external_printing_id,
            printing_external.printing_id,
            NULLIF(source.payload->>'name', ''),
            NULLIF(source.payload->>'type_line', ''),
            CASE
                WHEN jsonb_typeof(source.payload->'colors') = 'array'
                THEN source.payload->'colors'
                ELSE NULL
            END,
            CASE
                WHEN jsonb_typeof(source.payload->'colors') = 'array'
                THEN (
                    SELECT string_agg(color.value, '' ORDER BY color.ordinality)
                    FROM jsonb_array_elements_text(source.payload->'colors')
                        WITH ORDINALITY AS color(value, ordinality)
                )
                ELSE NULL
            END,
            CASE
                WHEN jsonb_typeof(source.payload->'color_identity') = 'array'
                THEN source.payload->'color_identity'
                ELSE NULL
            END,
            CASE
                WHEN jsonb_typeof(source.payload->'mana_value') = 'number'
                THEN (source.payload->>'mana_value')::double precision
                ELSE NULL
            END,
            NULLIF(source.payload->>'mana_cost', ''),
            NULLIF(source.payload->>'oracle_text', ''),
            source.external_oracle_id,
            CASE
                WHEN jsonb_typeof(source.payload->'legalities') = 'object'
                THEN source.payload->'legalities'
                ELSE NULL
            END,
            %s
        FROM _scryfall_printing_stage AS source
        JOIN catalog.external_ids AS printing_external
          ON printing_external.game_id = %s
         AND printing_external.provider_id = %s
         AND printing_external.entity_type = 'printing'
         AND printing_external.external_id_type = 'scryfall_id'
         AND printing_external.external_id = source.external_printing_id
        ON CONFLICT (game_id, provider_id, external_printing_id)
        DO UPDATE SET
            printing_id = EXCLUDED.printing_id,
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
    """, (
        GAME_ID, PROVIDER_ID, build_timestamp, GAME_ID, PROVIDER_ID,
    ))


def import_printings(
    conn,
    path: Path,
    build_updated_at: str,
    batch_size: int = 2000,
) -> dict[str, int]:
    """Stream and bulk-import Scryfall default_cards into PostgreSQL.

    Each bounded batch is staged with COPY and merged using set-based SQL.
    All batches remain inside one transaction: an invalid or failed import
    rolls back the complete snapshot. The supplied connection remains owned
    by the caller.
    """
    batch_size = max(1, min(10000, int(batch_size)))
    build_timestamp = _source_timestamp(build_updated_at)
    imported = 0
    with_images = 0
    batch: list[dict[str, Any]] = []

    with conn.transaction():
        conn.execute(
            "SELECT pg_advisory_xact_lock(%s)",
            (SCRYFALL_IMPORT_LOCK,),
        )
        _create_printing_staging(conn)

        def flush_batch() -> None:
            nonlocal imported, with_images, batch
            if not batch:
                return

            with conn.cursor() as cursor:
                with cursor.copy("""
                    COPY _scryfall_printing_raw (payload) FROM STDIN
                """) as copy:
                    for record in batch:
                        copy.write_row((Jsonb(record),))

            _prepare_printing_batch(conn)

            duplicate = conn.execute("""
                SELECT stage.external_printing_id
                FROM _scryfall_printing_stage AS stage
                JOIN _scryfall_printing_seen AS seen
                  USING (external_printing_id)
                LIMIT 1
            """).fetchone()
            if duplicate:
                raise RuntimeError(
                    "Scryfall printing export contains duplicate IDs "
                    "across batches: "
                    f"{duplicate['external_printing_id']}"
                )

            conn.execute("""
                INSERT INTO _scryfall_printing_seen (external_printing_id)
                SELECT external_printing_id
                FROM _scryfall_printing_stage
            """)

            _merge_printing_batch(conn, build_timestamp)

            imported += len(batch)
            with_images += sum(
                1 for record in batch if _image_for_card(record)
            )
            batch.clear()

            if imported % 10000 == 0:
                print(
                    f"[scryfall-postgres] staged and merged "
                    f"{imported:,} printings..."
                )

            conn.execute("TRUNCATE _scryfall_printing_raw")

        for record in iter_bulk_records(Path(path)):
            if not isinstance(record, dict):
                continue
            if not _source_id(record.get("id")):
                continue

            # Fail early on malformed dates rather than failing halfway
            # through a large transaction in PostgreSQL.
            _source_date(record.get("released_at"))

            batch.append(record)
            if len(batch) >= batch_size:
                flush_batch()

        flush_batch()

        if imported == 0:
            raise RuntimeError(
                "Scryfall default_cards export contained no printing records."
            )

        conn.execute("""
            UPDATE catalog.game_catalog_state
            SET revision = revision + 1, updated_at = now()
            WHERE game_id = %s
        """, (GAME_ID,))

    return {"printings": imported, "with_images": with_images}


def _tag_metadata(tag: dict[str, Any], source_version: str | None) -> dict[str, Any]:
    excluded = {
        "id", "slug", "label", "description", "parent_ids",
        "child_ids", "aliases", "taggings",
    }
    extras = {key: value for key, value in tag.items() if key not in excluded}
    return {
        "source": "scryfall",
        "source_version": source_version,
        "source_fields": extras,
    }


def _create_tag_staging(conn) -> None:
    # One raw JSONB staging table allows a single COPY stream per source file.
    # Normalized staging tables are populated with set-based SQL afterwards.
    for name in (
        "_scryfall_tag_raw",
        "_scryfall_tag_stage",
        "_scryfall_alias_stage",
        "_scryfall_art_assignment_stage",
        "_scryfall_oracle_assignment_stage",
    ):
        conn.execute(f"DROP TABLE IF EXISTS {name}")

    conn.execute("""
        CREATE TEMP TABLE _scryfall_tag_raw (
            stage_row BIGINT GENERATED ALWAYS AS IDENTITY,
            tag_type TEXT NOT NULL,
            payload JSONB NOT NULL,
            metadata JSONB NOT NULL
        ) ON COMMIT DROP
    """)
    conn.execute("""
        CREATE TEMP TABLE _scryfall_tag_stage (
            stage_row BIGINT GENERATED ALWAYS AS IDENTITY,
            tag_type TEXT NOT NULL,
            external_tag_id TEXT NOT NULL,
            slug TEXT NOT NULL,
            label TEXT NOT NULL,
            description TEXT,
            parent_ids JSONB NOT NULL,
            child_ids JSONB NOT NULL,
            metadata JSONB NOT NULL
        ) ON COMMIT DROP
    """)
    conn.execute("""
        CREATE TEMP TABLE _scryfall_alias_stage (
            stage_row BIGINT GENERATED ALWAYS AS IDENTITY,
            tag_type TEXT NOT NULL,
            external_tag_id TEXT NOT NULL,
            alias TEXT NOT NULL
        ) ON COMMIT DROP
    """)
    conn.execute("""
        CREATE TEMP TABLE _scryfall_art_assignment_stage (
            stage_row BIGINT GENERATED ALWAYS AS IDENTITY,
            external_tag_id TEXT NOT NULL,
            external_illustration_id TEXT NOT NULL,
            weight TEXT,
            annotation TEXT
        ) ON COMMIT DROP
    """)
    conn.execute("""
        CREATE TEMP TABLE _scryfall_oracle_assignment_stage (
            stage_row BIGINT GENERATED ALWAYS AS IDENTITY,
            external_tag_id TEXT NOT NULL,
            external_oracle_id TEXT NOT NULL,
            weight TEXT,
            annotation TEXT
        ) ON COMMIT DROP
    """)


def _stage_tag_file(
    conn,
    kind: str,
    path: Path,
    source_version: str | None,
) -> None:
    # Never open simultaneous COPY streams on one PostgreSQL connection.
    with conn.cursor() as cursor:
        with cursor.copy("""
            COPY _scryfall_tag_raw (tag_type, payload, metadata)
            FROM STDIN
        """) as copy:
            for tag in iter_bulk_records(Path(path)):
                if not isinstance(tag, dict):
                    continue

                external_id = _source_id(tag.get("id"))
                slug = _source_id(tag.get("slug"))
                label = _source_id(tag.get("label"))
                if not external_id or not slug or not label:
                    continue

                normalized = dict(tag)
                normalized["id"] = external_id
                normalized["slug"] = slug
                normalized["label"] = label

                for field in ("parent_ids", "child_ids"):
                    values = normalized.get(field)
                    if values is None:
                        values = []
                    if not isinstance(values, list):
                        raise RuntimeError(
                            f"Scryfall {kind}_tags has malformed {field} "
                            f"for tag {external_id}."
                        )
                    normalized[field] = [
                        value
                        for value in (_source_id(item) for item in values)
                        if value
                    ]

                aliases = normalized.get("aliases")
                if not isinstance(aliases, list):
                    aliases = []
                normalized["aliases"] = [
                    value
                    for value in (_source_id(item) for item in aliases)
                    if value
                ]

                taggings = normalized.get("taggings")
                if taggings is None:
                    taggings = []
                if not isinstance(taggings, list):
                    raise RuntimeError(
                        f"Scryfall {kind}_tags has malformed taggings "
                        f"for tag {external_id}."
                    )

                valid_taggings = []
                id_field = (
                    "illustration_id" if kind == "art" else "oracle_id"
                )
                for tagging in taggings:
                    if not isinstance(tagging, dict):
                        continue
                    entity_id = _source_id(tagging.get(id_field))
                    if not entity_id:
                        continue
                    item = dict(tagging)
                    item[id_field] = entity_id
                    valid_taggings.append(item)
                normalized["taggings"] = valid_taggings

                copy.write_row((
                    kind,
                    Jsonb(normalized),
                    Jsonb(_tag_metadata(normalized, source_version)),
                ))

    # Expand the raw record arrays into normalized staging tables. These
    # statements operate entirely inside the caller's transaction.
    conn.execute("""
        INSERT INTO _scryfall_tag_stage (
            tag_type, external_tag_id, slug, label, description,
            parent_ids, child_ids, metadata
        )
        SELECT
            tag_type,
            payload->>'id',
            payload->>'slug',
            payload->>'label',
            payload->>'description',
            COALESCE(payload->'parent_ids', '[]'::jsonb),
            COALESCE(payload->'child_ids', '[]'::jsonb),
            metadata
        FROM _scryfall_tag_raw
        WHERE tag_type = %s
    """, (kind,))

    conn.execute("""
        INSERT INTO _scryfall_alias_stage (
            tag_type, external_tag_id, alias
        )
        SELECT
            raw.tag_type,
            raw.payload->>'id',
            btrim(alias.value)
        FROM _scryfall_tag_raw AS raw
        CROSS JOIN LATERAL
            jsonb_array_elements_text(raw.payload->'aliases') AS alias(value)
        WHERE raw.tag_type = %s
          AND btrim(alias.value) <> ''
    """, (kind,))

    if kind == "art":
        conn.execute("""
            INSERT INTO _scryfall_art_assignment_stage (
                external_tag_id, external_illustration_id, weight, annotation
            )
            SELECT
                raw.payload->>'id',
                tagging.value->>'illustration_id',
                COALESCE(
                    NULLIF(tagging.value->>'weight', ''),
                    'median'
                ),
                tagging.value->>'annotation'
            FROM _scryfall_tag_raw AS raw
            CROSS JOIN LATERAL
                jsonb_array_elements(raw.payload->'taggings') AS tagging(value)
            WHERE raw.tag_type = 'art'
              AND NULLIF(
                    btrim(tagging.value->>'illustration_id'), ''
                  ) IS NOT NULL
        """)
    else:
        conn.execute("""
            INSERT INTO _scryfall_oracle_assignment_stage (
                external_tag_id, external_oracle_id, weight, annotation
            )
            SELECT
                raw.payload->>'id',
                tagging.value->>'oracle_id',
                COALESCE(
                    NULLIF(tagging.value->>'weight', ''),
                    'median'
                ),
                tagging.value->>'annotation'
            FROM _scryfall_tag_raw AS raw
            CROSS JOIN LATERAL
                jsonb_array_elements(raw.payload->'taggings') AS tagging(value)
            WHERE raw.tag_type = 'oracle'
              AND NULLIF(
                    btrim(tagging.value->>'oracle_id'), ''
                  ) IS NOT NULL
        """)


def _validate_tag_staging(conn) -> dict[str, int]:
    from collections import defaultdict, deque

    counts = conn.execute("""
        SELECT
            COUNT(*) AS tags,
            COUNT(*) FILTER (WHERE tag_type = 'art') AS art_tags,
            COUNT(*) FILTER (WHERE tag_type = 'oracle') AS oracle_tags
        FROM _scryfall_tag_stage
    """).fetchone()

    if not counts["art_tags"]:
        raise RuntimeError("Scryfall art_tags export contained no valid tags.")
    if not counts["oracle_tags"]:
        raise RuntimeError("Scryfall oracle_tags export contained no valid tags.")

    duplicate = conn.execute("""
        SELECT tag_type, external_tag_id
        FROM _scryfall_tag_stage
        GROUP BY tag_type, external_tag_id
        HAVING COUNT(*) > 1
        LIMIT 1
    """).fetchone()
    if duplicate:
        raise RuntimeError(
            "Scryfall tag snapshot contains duplicate IDs: "
            f"{duplicate['tag_type']}:{duplicate['external_tag_id']}"
        )

    cross_type_duplicate = conn.execute("""
        SELECT external_tag_id
        FROM _scryfall_tag_stage
        GROUP BY external_tag_id
        HAVING COUNT(DISTINCT tag_type) > 1
        LIMIT 1
    """).fetchone()
    if cross_type_duplicate:
        raise RuntimeError(
            "Scryfall reused a tag ID across art and Oracle datasets: "
            f"{cross_type_duplicate['external_tag_id']}"
        )

    art_count = conn.execute("""
        SELECT COUNT(*) FROM (
            SELECT DISTINCT external_tag_id, external_illustration_id
            FROM _scryfall_art_assignment_stage
        ) AS assignments
    """).fetchone()["count"]

    oracle_count = conn.execute("""
        SELECT COUNT(*) FROM (
            SELECT DISTINCT external_tag_id, external_oracle_id
            FROM _scryfall_oracle_assignment_stage
        ) AS assignments
    """).fetchone()["count"]

    if not art_count:
        raise RuntimeError(
            "Scryfall art_tags export contained no illustration taggings."
        )
    if not oracle_count:
        raise RuntimeError(
            "Scryfall oracle_tags export contained no Oracle taggings."
        )

    missing_parent = conn.execute("""
        SELECT child.external_tag_id, child.tag_type, parent.value
        FROM _scryfall_tag_stage AS child
        CROSS JOIN LATERAL jsonb_array_elements_text(child.parent_ids)
            AS parent(value)
        LEFT JOIN _scryfall_tag_stage AS ancestor
          ON ancestor.tag_type = child.tag_type
         AND ancestor.external_tag_id = parent.value
        WHERE ancestor.external_tag_id IS NULL
        LIMIT 1
    """).fetchone()
    if missing_parent:
        raise RuntimeError(
            "Scryfall tag hierarchy references a missing parent: "
            f"{missing_parent['tag_type']} tag "
            f"{missing_parent['external_tag_id']} references "
            f"{missing_parent['value']}."
        )

    # Detect cycles iteratively; this avoids Python recursion limits and
    # ensures the recursive SQL closure query below is guaranteed to finish.
    rows = conn.execute("""
        SELECT tag_type, external_tag_id, parent_ids
        FROM _scryfall_tag_stage
    """).fetchall()

    known = {
        (row["tag_type"], row["external_tag_id"])
        for row in rows
    }
    parents_by_tag = {}
    children_by_tag = defaultdict(list)
    indegree = {}

    for row in rows:
        key = (row["tag_type"], row["external_tag_id"])
        parents = {
            (row["tag_type"], value)
            for value in row["parent_ids"]
        }
        for parent in parents:
            if parent not in known:
                raise RuntimeError(
                    f"Scryfall hierarchy parent not found: {parent[1]}"
                )
            children_by_tag[parent].append(key)
        parents_by_tag[key] = parents
        indegree[key] = len(parents)

    queue = deque(key for key, degree in indegree.items() if degree == 0)
    processed = 0
    while queue:
        parent = queue.popleft()
        processed += 1
        for child in children_by_tag[parent]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)

    if processed != len(known):
        raise RuntimeError("Scryfall tag hierarchy contains a cycle.")

    return {
        "tags": int(counts["tags"]),
        "art_assignments": int(art_count),
        "oracle_assignments": int(oracle_count),
    }


def _ensure_tag_illustrations(conn) -> None:
    wrong_type = conn.execute("""
        SELECT a.external_illustration_id, e.entity_type
        FROM (
            SELECT DISTINCT external_illustration_id
            FROM _scryfall_art_assignment_stage
        ) AS a
        JOIN catalog.external_ids AS e
          ON e.game_id = %s
         AND e.provider_id = %s
         AND e.external_id_type = 'illustration_id'
         AND e.external_id = a.external_illustration_id
        WHERE e.entity_type <> 'illustration'
        LIMIT 1
    """, (GAME_ID, PROVIDER_ID)).fetchone()

    if wrong_type:
        raise RuntimeError(
            "Scryfall illustration ID is already mapped to another entity: "
            f"{wrong_type['external_illustration_id']}"
        )

    conn.execute("""
        WITH wanted AS (
            SELECT DISTINCT external_illustration_id
            FROM _scryfall_art_assignment_stage
        ),
        missing AS (
            SELECT wanted.external_illustration_id
            FROM wanted
            LEFT JOIN catalog.external_ids AS e
              ON e.game_id = %s
             AND e.provider_id = %s
             AND e.entity_type = 'illustration'
             AND e.external_id_type = 'illustration_id'
             AND e.external_id = wanted.external_illustration_id
            WHERE e.external_id_id IS NULL
        ),
        created AS (
            INSERT INTO catalog.illustrations (game_id, metadata)
            SELECT
                %s,
                jsonb_build_object(
                    'source', 'scryfall',
                    'external_illustration_id', external_illustration_id
                )
            FROM missing
            RETURNING illustration_id, metadata
        )
        INSERT INTO catalog.external_ids (
            game_id, provider_id, entity_type,
            external_id_type, external_id, illustration_id
        )
        SELECT
            %s, %s, 'illustration', 'illustration_id',
            metadata->>'external_illustration_id', illustration_id
        FROM created
        ON CONFLICT (
            game_id, provider_id, external_id_type, external_id
        ) DO NOTHING
    """, (
        GAME_ID, PROVIDER_ID, GAME_ID, GAME_ID, PROVIDER_ID,
    ))

    unresolved = conn.execute("""
        SELECT a.external_illustration_id
        FROM (
            SELECT DISTINCT external_illustration_id
            FROM _scryfall_art_assignment_stage
        ) AS a
        LEFT JOIN catalog.external_ids AS e
          ON e.game_id = %s
         AND e.provider_id = %s
         AND e.entity_type = 'illustration'
         AND e.external_id_type = 'illustration_id'
         AND e.external_id = a.external_illustration_id
        WHERE e.illustration_id IS NULL
        LIMIT 1
    """, (GAME_ID, PROVIDER_ID)).fetchone()

    if unresolved:
        raise RuntimeError(
            "Could not resolve Scryfall illustration identity: "
            f"{unresolved['external_illustration_id']}"
        )


def _replace_tag_snapshot(conn, versions: dict[str, str]) -> dict[str, int]:
    # Artwork/Oracle assignments and hierarchy are snapshot-owned. Remove
    # only the Scryfall/MTG slice; other games and providers are untouched.
    conn.execute("""
        DELETE FROM catalog.art_tag_assignments
        WHERE game_id = %s AND provider_id = %s
    """, (GAME_ID, PROVIDER_ID))
    conn.execute("""
        DELETE FROM catalog.oracle_tag_assignments
        WHERE game_id = %s AND provider_id = %s
    """, (GAME_ID, PROVIDER_ID))
    conn.execute("""
        DELETE FROM catalog.tag_aliases
        WHERE game_id = %s AND provider_id = %s
    """, (GAME_ID, PROVIDER_ID))
    conn.execute("""
        DELETE FROM catalog.tag_closure
        WHERE game_id = %s AND provider_id = %s
    """, (GAME_ID, PROVIDER_ID))

    # Upsert source identities, preserving internal IDs for tags that remain.
    conn.execute("""
        INSERT INTO catalog.tags (
            game_id, provider_id, external_tag_id, tag_type,
            slug, label, description, parent_ids, child_ids, metadata
        )
        SELECT
            %s, %s, external_tag_id, tag_type,
            slug, label, description, parent_ids, child_ids, metadata
        FROM _scryfall_tag_stage
        ORDER BY stage_row
        ON CONFLICT (game_id, provider_id, external_tag_id)
        DO UPDATE SET
            tag_type = EXCLUDED.tag_type,
            slug = EXCLUDED.slug,
            label = EXCLUDED.label,
            description = EXCLUDED.description,
            parent_ids = EXCLUDED.parent_ids,
            child_ids = EXCLUDED.child_ids,
            metadata = EXCLUDED.metadata,
            updated_at = now()
    """, (GAME_ID, PROVIDER_ID))

    # Remove old tags no longer present in either current source dataset.
    conn.execute("""
        DELETE FROM catalog.tags AS live
        WHERE live.game_id = %s
          AND live.provider_id = %s
          AND NOT EXISTS (
              SELECT 1
              FROM _scryfall_tag_stage AS staged
              WHERE staged.external_tag_id = live.external_tag_id
          )
    """, (GAME_ID, PROVIDER_ID))

    aliases = conn.execute("""
        INSERT INTO catalog.tag_aliases (
            tag_id, game_id, provider_id, alias
        )
        SELECT
            t.tag_id, %s, %s, a.alias
        FROM (
            SELECT DISTINCT ON (
                tag_type, external_tag_id, lower(btrim(alias))
            )
                tag_type, external_tag_id, alias
            FROM _scryfall_alias_stage
            ORDER BY
                tag_type, external_tag_id, lower(btrim(alias)), stage_row
        ) AS a
        JOIN catalog.tags AS t
          ON t.game_id = %s
         AND t.provider_id = %s
         AND t.tag_type = a.tag_type
         AND t.external_tag_id = a.external_tag_id
        ON CONFLICT DO NOTHING
    """, (GAME_ID, PROVIDER_ID, GAME_ID, PROVIDER_ID)).rowcount

    art_assignments = conn.execute("""
        INSERT INTO catalog.art_tag_assignments (
            game_id, provider_id, tag_type, tag_id,
            illustration_id, weight, annotation
        )
        SELECT
            %s, %s, 'art', t.tag_id,
            e.illustration_id, a.weight, a.annotation
        FROM (
            SELECT DISTINCT ON (
                external_tag_id, external_illustration_id
            )
                external_tag_id, external_illustration_id,
                weight, annotation
            FROM _scryfall_art_assignment_stage
            ORDER BY
                external_tag_id, external_illustration_id, stage_row
        ) AS a
        JOIN catalog.tags AS t
          ON t.game_id = %s
         AND t.provider_id = %s
         AND t.tag_type = 'art'
         AND t.external_tag_id = a.external_tag_id
        JOIN catalog.external_ids AS e
          ON e.game_id = %s
         AND e.provider_id = %s
         AND e.entity_type = 'illustration'
         AND e.external_id_type = 'illustration_id'
         AND e.external_id = a.external_illustration_id
    """, (
        GAME_ID, PROVIDER_ID, GAME_ID, PROVIDER_ID, GAME_ID, PROVIDER_ID,
    )).rowcount

    oracle_assignments = conn.execute("""
        INSERT INTO catalog.oracle_tag_assignments (
            game_id, provider_id, tag_type, tag_id,
            external_oracle_id, card_id, weight, annotation
        )
        SELECT
            %s, %s, 'oracle', t.tag_id,
            a.external_oracle_id, c.card_id, a.weight, a.annotation
        FROM (
            SELECT DISTINCT ON (external_tag_id, external_oracle_id)
                external_tag_id, external_oracle_id, weight, annotation
            FROM _scryfall_oracle_assignment_stage
            ORDER BY external_tag_id, external_oracle_id, stage_row
        ) AS a
        JOIN catalog.tags AS t
          ON t.game_id = %s
         AND t.provider_id = %s
         AND t.tag_type = 'oracle'
         AND t.external_tag_id = a.external_tag_id
        LEFT JOIN catalog.external_ids AS c
          ON c.game_id = %s
         AND c.provider_id = %s
         AND c.entity_type = 'card'
         AND c.external_id_type = 'oracle_id'
         AND c.external_id = a.external_oracle_id
    """, (
        GAME_ID, PROVIDER_ID, GAME_ID, PROVIDER_ID, GAME_ID, PROVIDER_ID,
    )).rowcount

    # The graph was validated as a DAG before any live rows were replaced.
    # Recursive traversal records the shortest depth for each ancestor pair.
    conn.execute("""
        WITH RECURSIVE closure AS (
            SELECT
                child.tag_type,
                parent.value AS ancestor_external_id,
                child.external_tag_id AS descendant_external_id,
                1 AS depth
            FROM _scryfall_tag_stage AS child
            CROSS JOIN LATERAL
                jsonb_array_elements_text(child.parent_ids) AS parent(value)

            UNION ALL

            SELECT
                closure.tag_type,
                parent.value,
                closure.descendant_external_id,
                closure.depth + 1
            FROM closure
            JOIN _scryfall_tag_stage AS ancestor
              ON ancestor.tag_type = closure.tag_type
             AND ancestor.external_tag_id = closure.ancestor_external_id
            CROSS JOIN LATERAL
                jsonb_array_elements_text(ancestor.parent_ids) AS parent(value)
        )
        INSERT INTO catalog.tag_closure (
            game_id, provider_id, tag_type,
            ancestor_tag_id, descendant_tag_id, depth
        )
        SELECT
            %s, %s, closure.tag_type,
            ancestor.tag_id, descendant.tag_id, MIN(closure.depth)
        FROM closure
        JOIN catalog.tags AS ancestor
          ON ancestor.game_id = %s
         AND ancestor.provider_id = %s
         AND ancestor.tag_type = closure.tag_type
         AND ancestor.external_tag_id = closure.ancestor_external_id
        JOIN catalog.tags AS descendant
          ON descendant.game_id = %s
         AND descendant.provider_id = %s
         AND descendant.tag_type = closure.tag_type
         AND descendant.external_tag_id = closure.descendant_external_id
        GROUP BY
            closure.tag_type, ancestor.tag_id, descendant.tag_id
    """, (
        GAME_ID, PROVIDER_ID,
        GAME_ID, PROVIDER_ID,
        GAME_ID, PROVIDER_ID,
    ))

    conn.execute("""
        UPDATE catalog.game_catalog_state
        SET revision = revision + 1, updated_at = now()
        WHERE game_id = %s
    """, (GAME_ID,))

    return {
        "tags": int(conn.execute("""
            SELECT COUNT(*) FROM _scryfall_tag_stage
        """).fetchone()["count"]),
        "aliases": int(aliases),
        "art_assignments": int(art_assignments),
        "oracle_assignments": int(oracle_assignments),
    }


def import_tag_snapshots(
    conn,
    art_path: Path,
    oracle_path: Path,
    versions: dict[str, str] | None = None,
) -> dict[str, int]:
    """Atomically replace the MTG/Scryfall art and Oracle tag snapshots.

    The supplied connection and caller's transaction remain caller-owned.
    Missing parents, cycles, empty exports, and import errors leave the previous
    live snapshot intact. Unresolved illustration and Oracle IDs are retained.
    """
    versions = versions or {}

    with conn.transaction():
        conn.execute(
            "SELECT pg_advisory_xact_lock(%s)",
            (SCRYFALL_IMPORT_LOCK,),
        )
        _create_tag_staging(conn)

        _stage_tag_file(
            conn, "art", Path(art_path), _source_id(versions.get("art_tags"))
        )
        _stage_tag_file(
            conn, "oracle", Path(oracle_path),
            _source_id(versions.get("oracle_tags")),
        )

        _validate_tag_staging(conn)
        _ensure_tag_illustrations(conn)

        return _replace_tag_snapshot(conn, versions)



def _store_image_hash(
    conn,
    image_id: int,
    expected_uri: str,
    hash_value: str,
) -> bool:
    """Store a pHash only if the image still has the URI that was hashed.

    Locking the image row serializes this write against concurrent changes to
    the image resource. The caller owns the transaction and connection.
    """
    if not expected_uri or not hash_value:
        return False

    current = conn.execute(
        """
        SELECT image_id
        FROM catalog.images
        WHERE image_id = %s
          AND game_id = %s
          AND provider_id = %s
          AND image_uri = %s
        FOR UPDATE
        """,
        (image_id, GAME_ID, PROVIDER_ID, expected_uri),
    ).fetchone()

    if current is None:
        return False

    saved = conn.execute(
        """
        INSERT INTO catalog.image_hashes (
            image_id, game_id, hash_algorithm,
            hash_version, hash_value, computed_at, metadata
        )
        VALUES (%s, %s, 'phash', '1', %s, now(), %s)
        ON CONFLICT (image_id, hash_algorithm, hash_version)
        DO UPDATE SET
            hash_value = EXCLUDED.hash_value,
            computed_at = EXCLUDED.computed_at,
            metadata = EXCLUDED.metadata
        RETURNING image_id
        """,
        (
            image_id,
            GAME_ID,
            hash_value,
            Jsonb({
                "source": "scryfall",
                "source_image_uri": expected_uri,
            }),
        ),
    ).fetchone()

    return saved is not None


def fill_missing_hashes(
    conn,
    limit: int | None = None,
    workers: int | None = None,
    batch_size: int = 128,
    image_ids: list[int] | tuple[int, ...] | None = None,
) -> dict[str, int]:
    """Generate missing pHashes with bounded database and worker batches.

    Only one bounded batch of image records and futures exists at a time.
    Downloads execute concurrently; PostgreSQL writes stay on the caller's
    thread and connection. The caller owns the transaction and commit.
    """
    selected_image_ids = None
    if image_ids is not None:
        selected_image_ids = tuple(sorted({int(value) for value in image_ids}))
        if any(value <= 0 for value in selected_image_ids):
            raise ValueError("image_ids must contain positive database IDs")
        if not selected_image_ids:
            return {"attempted": 0, "updated": 0, "failed": 0}

    if limit is not None:
        limit = max(0, int(limit))
        if limit == 0:
            return {"attempted": 0, "updated": 0, "failed": 0}

    batch_size = max(1, min(1000, int(batch_size)))
    worker_count = max(
        1,
        min(8, int(IMAGE_WORKERS if workers is None else workers)),
    )

    attempted = 0
    updated = 0
    failed = 0
    last_image_id = -9223372036854775808

    print(
        f"[scryfall-postgres] hashing missing image resources "
        f"in batches of {batch_size:,} with {worker_count} workers..."
    )

    query = """
        SELECT i.image_id, i.image_uri
        FROM catalog.images AS i
        LEFT JOIN catalog.image_hashes AS h
          ON h.image_id = i.image_id
         AND h.game_id = i.game_id
         AND h.hash_algorithm = 'phash'
         AND h.hash_version = '1'
        WHERE i.game_id = %s
          AND i.provider_id = %s
          AND h.image_id IS NULL
          AND i.image_id > %s
        ORDER BY i.image_id
        LIMIT %s
    """

    if selected_image_ids is not None:
        query = query.replace(
            "          AND i.image_id > %s",
            "          AND i.image_id > %s\n          AND i.image_id = ANY(%s::bigint[])",
            1,
        )

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        while limit is None or attempted < limit:
            fetch_size = batch_size
            if limit is not None:
                fetch_size = min(fetch_size, limit - attempted)

            query_params = (GAME_ID, PROVIDER_ID, last_image_id)
            if selected_image_ids is not None:
                query_params += (list(selected_image_ids),)
            query_params += (fetch_size,)

            rows = conn.execute(query, query_params).fetchall()

            if not rows:
                break

            # Advance the keyset even when downloads fail. Failures remain
            # unhashed and can be retried by a subsequent worker invocation.
            last_image_id = max(int(row["image_id"]) for row in rows)
            attempted += len(rows)

            futures = {
                pool.submit(hash_image, str(row["image_uri"])):
                    (int(row["image_id"]), str(row["image_uri"]))
                for row in rows
            }

            for future in as_completed(futures):
                image_id, expected_uri = futures[future]

                try:
                    returned_uri, hash_value, error = future.result()
                except Exception as exc:
                    returned_uri, hash_value, error = expected_uri, None, str(exc)

                if returned_uri != expected_uri:
                    hash_value = None
                    error = "hash worker returned a different image URI"

                if not hash_value:
                    failed += 1
                    if failed <= 20:
                        print(
                            f"[scryfall-postgres] image hash failed for "
                            f"image {image_id}: {error or 'no hash returned'}"
                        )
                    continue

                stored = _store_image_hash(
                    conn, image_id, expected_uri, str(hash_value)
                )

                if stored:
                    updated += 1
                else:
                    failed += 1
                    if failed <= 20:
                        print(
                            f"[scryfall-postgres] discarded stale hash result "
                            f"for image {image_id}"
                        )

            print(
                f"[scryfall-postgres] image hashes processed {attempted:,} "
                f"(updated {updated:,}, failed {failed:,})"
            )

    return {
        "attempted": attempted,
        "updated": updated,
        "failed": failed,
    }
