"""Export the PostgreSQL MTG catalog as the legacy desktop-scanner SQLite file.

The PostgreSQL catalog is authoritative in PostgreSQL mode. This module creates
an atomic, read-only-derived SQLite compatibility artifact; it never reads or
modifies the production SQLite catalog.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Callable, Iterable
from uuid import uuid4

from scryfall_catalog import ensure_art_catalog_schema


BATCH_SIZE = 2000

CARD_ART_COLUMNS = (
    "scryfall_id",
    "name",
    "set_code",
    "set_name",
    "rarity",
    "tcgplayer_id",
    "collector_number",
    "phash",
    "oracle_id",
    "illustration_id",
    "image_uri",
    "last_seen_build",
)
FACE_COLUMNS = (
    "scryfall_id",
    "face_index",
    "oracle_id",
    "illustration_id",
    "image_uri",
)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _json_text(value: Any) -> str:
    if value is None:
        value = []
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _rows(conn, query: str, params: tuple[Any, ...] = ()):
    """Stream rows from a psycopg or PostgresAppConnection cursor."""
    cursor = conn.execute(query, params)
    try:
        while True:
            batch = cursor.fetchmany(BATCH_SIZE)
            if not batch:
                break
            yield from batch
    finally:
        cursor.close()


def _insert_stream(
    db: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
    rows: Iterable,
    convert: Callable[[Any], tuple[Any, ...]] | None = None,
) -> int:
    names = ", ".join(f'"{column}"' for column in columns)
    placeholders = ", ".join("?" for _ in columns)
    statement = f'INSERT INTO "{table}" ({names}) VALUES ({placeholders})'
    inserted = 0
    batch: list[tuple[Any, ...]] = []
    for row in rows:
        batch.append(convert(row) if convert else tuple(row))
        if len(batch) >= BATCH_SIZE:
            db.executemany(statement, batch)
            inserted += len(batch)
            batch.clear()
    if batch:
        db.executemany(statement, batch)
        inserted += len(batch)
    return inserted


_CARD_ART_SQL = """
SELECT
    printing_external.external_id AS scryfall_id,
    CASE
        WHEN printing.metadata ? 'name' THEN printing.metadata ->> 'name'
        ELSE COALESCE(provider_metadata.name, card.name, face.name)
    END AS name,
    CASE
        WHEN printing.metadata ? 'set_code' THEN printing.metadata ->> 'set_code'
        WHEN printing.metadata ? 'set' THEN printing.metadata ->> 'set'
        ELSE card_set.set_code
    END AS set_code,
    CASE
        WHEN printing.metadata ? 'set_name' THEN printing.metadata ->> 'set_name'
        ELSE card_set.name
    END AS set_name,
    CASE
        WHEN printing.metadata ? 'rarity' THEN printing.metadata ->> 'rarity'
        ELSE printing.rarity
    END AS rarity,
    CASE
        WHEN printing.metadata ? 'tcgplayer_id' THEN printing.metadata ->> 'tcgplayer_id'
        ELSE product_mapping.target_external_product_id
    END AS tcgplayer_id,
    CASE
        WHEN printing.metadata ? 'collector_number' THEN printing.metadata ->> 'collector_number'
        ELSE printing.collector_number
    END AS collector_number,
    COALESCE(image_hash.hash_value, printing.metadata ->> 'phash') AS phash,
    CASE
        WHEN printing.metadata ? 'oracle_id' THEN printing.metadata ->> 'oracle_id'
        ELSE COALESCE(provider_metadata.external_oracle_id, face.metadata ->> 'oracle_id')
    END AS oracle_id,
    CASE
        WHEN printing.metadata ? 'illustration_id' THEN printing.metadata ->> 'illustration_id'
        ELSE face_illustration.external_id
    END AS illustration_id,
    CASE
        WHEN printing.metadata ? 'image_uri' THEN printing.metadata ->> 'image_uri'
        ELSE COALESCE(
            printing_image.image_uri,
            face_image.image_uri,
            printing.metadata #>> '{image_uris,normal}'
        )
    END AS image_uri,
    CASE
        WHEN printing.metadata ? 'last_seen_build' THEN printing.metadata ->> 'last_seen_build'
        ELSE COALESCE(provider_metadata.updated_at::text, printing.updated_at::text)
    END AS last_seen_build
FROM catalog.printings AS printing
JOIN catalog.external_ids AS printing_external
  ON printing_external.game_id = printing.game_id
 AND printing_external.provider_id = 'scryfall'
 AND printing_external.entity_type = 'printing'
 AND printing_external.external_id_type = 'scryfall_id'
 AND printing_external.printing_id = printing.printing_id
LEFT JOIN catalog.sets AS card_set
  ON card_set.game_id = printing.game_id
 AND card_set.set_id = printing.set_id
LEFT JOIN catalog.cards AS card
  ON card.game_id = printing.game_id
 AND card.card_id = printing.card_id
LEFT JOIN catalog.provider_printing_metadata AS provider_metadata
  ON provider_metadata.game_id = printing.game_id
 AND provider_metadata.provider_id = 'scryfall'
 AND provider_metadata.printing_id = printing.printing_id
LEFT JOIN catalog.printing_faces AS face
  ON face.game_id = printing.game_id
 AND face.printing_id = printing.printing_id
 AND face.face_index = 0
LEFT JOIN catalog.images AS printing_image
  ON printing_image.game_id = printing.game_id
 AND printing_image.image_id = printing.image_id
LEFT JOIN catalog.images AS face_image
  ON face_image.game_id = face.game_id
 AND face_image.image_id = face.image_id
LEFT JOIN catalog.image_hashes AS image_hash
  ON image_hash.game_id = printing.game_id
 AND image_hash.image_id = printing.image_id
 AND image_hash.hash_algorithm = 'phash'
 AND image_hash.hash_version = '1'
LEFT JOIN catalog.external_ids AS face_illustration
  ON face_illustration.game_id = face.game_id
 AND face_illustration.provider_id = 'scryfall'
 AND face_illustration.entity_type = 'illustration'
 AND face_illustration.external_id_type = 'illustration_id'
 AND face_illustration.illustration_id = face.illustration_id
LEFT JOIN catalog.printing_product_mappings AS product_mapping
  ON product_mapping.game_id = printing.game_id
 AND product_mapping.source_provider_id = 'scryfall'
 AND product_mapping.source_external_printing_id = printing_external.external_id
 AND product_mapping.target_provider_id = 'tcgcsv'
WHERE printing.game_id = 'mtg'
ORDER BY printing_external.external_id
"""

_FACE_SQL = """
SELECT
    printing_external.external_id AS scryfall_id,
    face.face_index,
    CASE
        WHEN face.metadata ? 'oracle_id' THEN face.metadata ->> 'oracle_id'
        ELSE provider_metadata.external_oracle_id
    END AS oracle_id,
    CASE
        WHEN face.metadata ? 'illustration_id' THEN face.metadata ->> 'illustration_id'
        ELSE illustration.external_id
    END AS illustration_id,
    CASE
        WHEN face.metadata ? 'image_uri' THEN face.metadata ->> 'image_uri'
        ELSE COALESCE(
            face_image.image_uri,
            face.metadata #>> '{image_uris,normal}'
        )
    END AS image_uri
FROM catalog.printing_faces AS face
JOIN catalog.external_ids AS printing_external
  ON printing_external.game_id = face.game_id
 AND printing_external.provider_id = 'scryfall'
 AND printing_external.entity_type = 'printing'
 AND printing_external.external_id_type = 'scryfall_id'
 AND printing_external.printing_id = face.printing_id
LEFT JOIN catalog.provider_printing_metadata AS provider_metadata
  ON provider_metadata.game_id = face.game_id
 AND provider_metadata.provider_id = 'scryfall'
 AND provider_metadata.printing_id = face.printing_id
LEFT JOIN catalog.images AS face_image
  ON face_image.game_id = face.game_id
 AND face_image.image_id = face.image_id
LEFT JOIN catalog.external_ids AS illustration
  ON illustration.game_id = face.game_id
 AND illustration.provider_id = 'scryfall'
 AND illustration.entity_type = 'illustration'
 AND illustration.external_id_type = 'illustration_id'
 AND illustration.illustration_id = face.illustration_id
WHERE face.game_id = 'mtg'
  AND COALESCE((face.metadata ->> '_synthetic')::boolean, false) = false
ORDER BY printing_external.external_id, face.face_index
"""

_TAGS_SQL = """
SELECT external_tag_id AS id, slug, label, tag_type, description,
       parent_ids, child_ids
FROM catalog.tags
WHERE game_id = 'mtg' AND provider_id = 'scryfall'
ORDER BY external_tag_id
"""

_ALIASES_SQL = """
SELECT tag.external_tag_id AS tag_id, alias.alias
FROM catalog.tag_aliases AS alias
JOIN catalog.tags AS tag
  ON tag.tag_id = alias.tag_id
 AND tag.game_id = alias.game_id
 AND tag.provider_id = alias.provider_id
WHERE tag.game_id = 'mtg' AND tag.provider_id = 'scryfall'
ORDER BY tag.external_tag_id, alias.alias
"""

_CLOSURE_SQL = """
SELECT ancestor.external_tag_id AS ancestor_tag_id,
       descendant.external_tag_id AS descendant_tag_id,
       closure.depth
FROM catalog.tag_closure AS closure
JOIN catalog.tags AS ancestor
  ON ancestor.tag_id = closure.ancestor_tag_id
 AND ancestor.game_id = closure.game_id
 AND ancestor.provider_id = closure.provider_id
JOIN catalog.tags AS descendant
  ON descendant.tag_id = closure.descendant_tag_id
 AND descendant.game_id = closure.game_id
 AND descendant.provider_id = closure.provider_id
WHERE closure.game_id = 'mtg' AND closure.provider_id = 'scryfall'
ORDER BY ancestor.external_tag_id, descendant.external_tag_id
"""

_ART_ASSIGNMENTS_SQL = """
SELECT tag.external_tag_id AS tag_id,
       illustration.external_id AS illustration_id,
       assignment.weight,
       assignment.annotation
FROM catalog.art_tag_assignments AS assignment
JOIN catalog.tags AS tag
  ON tag.tag_id = assignment.tag_id
 AND tag.game_id = assignment.game_id
 AND tag.provider_id = assignment.provider_id
JOIN catalog.external_ids AS illustration
  ON illustration.game_id = assignment.game_id
 AND illustration.provider_id = assignment.provider_id
 AND illustration.entity_type = 'illustration'
 AND illustration.external_id_type = 'illustration_id'
 AND illustration.illustration_id = assignment.illustration_id
WHERE assignment.game_id = 'mtg'
  AND assignment.provider_id = 'scryfall'
ORDER BY tag.external_tag_id, illustration.external_id
"""

_ORACLE_ASSIGNMENTS_SQL = """
SELECT tag.external_tag_id AS tag_id,
       assignment.external_oracle_id AS oracle_id,
       assignment.weight,
       assignment.annotation
FROM catalog.oracle_tag_assignments AS assignment
JOIN catalog.tags AS tag
  ON tag.tag_id = assignment.tag_id
 AND tag.game_id = assignment.game_id
 AND tag.provider_id = assignment.provider_id
WHERE assignment.game_id = 'mtg'
  AND assignment.provider_id = 'scryfall'
ORDER BY tag.external_tag_id, assignment.external_oracle_id
"""


def export_mtg_legacy_art_catalog(conn, output_path: str | Path) -> dict[str, Any]:
    """Atomically write the seven-table MTG desktop-scanner SQLite artifact.

    The caller must run this function inside a PostgreSQL REPEATABLE READ,
    READ ONLY transaction so all exported tables represent one catalog revision.
    The caller owns the PostgreSQL connection and its transaction.

    The target is replaced only after all rows have been written and the
    generated SQLite file passes integrity and schema checks.
    """
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.{uuid4().hex}.tmp"
    )

    state = conn.execute(
        """
        SELECT revision, updated_at
        FROM catalog.game_catalog_state
        WHERE game_id = 'mtg'
        """
    ).fetchone()
    if not state:
        raise RuntimeError("PostgreSQL MTG catalog state is not initialized.")

    revision = int(state["revision"])
    updated_at = _text(state["updated_at"])

    counts: dict[str, int] = {}
    try:
        ensure_art_catalog_schema(temporary)
        db = sqlite3.connect(str(temporary), timeout=60)
        try:
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("PRAGMA journal_mode=DELETE")
            with db:
                counts["card_art"] = _insert_stream(
                    db,
                    "card_art",
                    CARD_ART_COLUMNS,
                    _rows(conn, _CARD_ART_SQL),
                    lambda row: tuple(
                        _text(row[column]) for column in CARD_ART_COLUMNS
                    ),
                )
                counts["card_art_faces"] = _insert_stream(
                    db,
                    "card_art_faces",
                    FACE_COLUMNS,
                    _rows(conn, _FACE_SQL),
                    lambda row: (
                        _text(row["scryfall_id"]),
                        int(row["face_index"]),
                        _text(row["oracle_id"]),
                        _text(row["illustration_id"]),
                        _text(row["image_uri"]),
                    ),
                )
                counts["scryfall_tags"] = _insert_stream(
                    db,
                    "scryfall_tags",
                    (
                        "id",
                        "slug",
                        "label",
                        "tag_type",
                        "description",
                        "parent_ids_json",
                        "child_ids_json",
                    ),
                    _rows(conn, _TAGS_SQL),
                    lambda row: (
                        _text(row["id"]),
                        _text(row["slug"]),
                        _text(row["label"]),
                        _text(row["tag_type"]),
                        _text(row["description"]),
                        _json_text(row["parent_ids"]),
                        _json_text(row["child_ids"]),
                    ),
                )
                counts["scryfall_tag_aliases"] = _insert_stream(
                    db,
                    "scryfall_tag_aliases",
                    ("tag_id", "alias"),
                    _rows(conn, _ALIASES_SQL),
                    lambda row: (
                        _text(row["tag_id"]),
                        _text(row["alias"]),
                    ),
                )
                counts["scryfall_tag_closure"] = _insert_stream(
                    db,
                    "scryfall_tag_closure",
                    ("ancestor_tag_id", "descendant_tag_id", "depth"),
                    _rows(conn, _CLOSURE_SQL),
                    lambda row: (
                        _text(row["ancestor_tag_id"]),
                        _text(row["descendant_tag_id"]),
                        int(row["depth"]),
                    ),
                )
                counts["art_tag_assignments"] = _insert_stream(
                    db,
                    "art_tag_assignments",
                    ("tag_id", "illustration_id", "weight", "annotation"),
                    _rows(conn, _ART_ASSIGNMENTS_SQL),
                    lambda row: (
                        _text(row["tag_id"]),
                        _text(row["illustration_id"]),
                        _text(row["weight"]),
                        _text(row["annotation"]),
                    ),
                )
                counts["oracle_tag_assignments"] = _insert_stream(
                    db,
                    "oracle_tag_assignments",
                    ("tag_id", "oracle_id", "weight", "annotation"),
                    _rows(conn, _ORACLE_ASSIGNMENTS_SQL),
                    lambda row: (
                        _text(row["tag_id"]),
                        _text(row["oracle_id"]),
                        _text(row["weight"]),
                        _text(row["annotation"]),
                    ),
                )

            integrity = db.execute("PRAGMA integrity_check").fetchone()
            if not integrity or str(integrity[0]).lower() != "ok":
                raise RuntimeError(
                    "The generated MTG art catalog failed SQLite integrity_check."
                )

            expected_columns = {
                "card_art": CARD_ART_COLUMNS,
                "card_art_faces": FACE_COLUMNS,
                "scryfall_tags": (
                    "id", "slug", "label", "tag_type", "description",
                    "parent_ids_json", "child_ids_json",
                ),
                "scryfall_tag_aliases": ("tag_id", "alias"),
                "scryfall_tag_closure": (
                    "ancestor_tag_id", "descendant_tag_id", "depth",
                ),
                "art_tag_assignments": (
                    "tag_id", "illustration_id", "weight", "annotation",
                ),
                "oracle_tag_assignments": (
                    "tag_id", "oracle_id", "weight", "annotation",
                ),
            }
            for table, expected in expected_columns.items():
                actual = tuple(
                    row[1]
                    for row in db.execute(
                        f'PRAGMA table_info("{table}")'
                    ).fetchall()
                )
                if actual != expected:
                    raise RuntimeError(
                        f"Generated {table} schema mismatch: {actual!r}"
                    )
                actual_count = int(
                    db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                )
                if actual_count != counts[table]:
                    raise RuntimeError(
                        f"Generated {table} row count mismatch: "
                        f"{actual_count} != {counts[table]}"
                    )
            counts["phash"] = int(
                db.execute(
                    "SELECT COUNT(*) FROM card_art "
                    "WHERE phash IS NOT NULL AND trim(phash) <> ''"
                ).fetchone()[0]
            )
        finally:
            db.close()

        os.chmod(temporary, 0o640)
        os.replace(temporary, destination)
    except BaseException:
        for candidate in (
            temporary,
            Path(str(temporary) + "-wal"),
            Path(str(temporary) + "-shm"),
        ):
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass
        raise

    return {
        "catalog_revision": revision,
        "source_updated_at": updated_at,
        **counts,
    }
