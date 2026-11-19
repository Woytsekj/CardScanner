#!/usr/bin/env python3
"""One-time, transactional SQLite backup -> CardScanner PostgreSQL migration."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path
from urllib.parse import urlparse

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb


SOURCE = Path.home() / "cardscanner-pg-migration-source-2026-11-13"
GAME = "mtg"
SCRYFALL = "scryfall"
TCGCSV = "tcgcsv"

TARGET_TABLES = [
    "app.app_migrations", "app.app_state", "app.card_aliases",
    "app.deck_cards", "app.decklists", "app.inventory",
    "app.login_attempts", "app.people", "app.price_alert_log",
    "app.scan_log", "app.users",
    "catalog.art_tag_assignments", "catalog.cards",
    "catalog.external_ids", "catalog.illustrations",
    "catalog.image_hashes", "catalog.images",
    "catalog.oracle_tag_assignments", "catalog.printing_faces",
    "catalog.printing_product_mappings", "catalog.printings",
    "catalog.printing_rulings_cache",
    "catalog.product_price_history",
    "catalog.provider_printing_metadata", "catalog.sets",
    "catalog.tag_aliases", "catalog.tag_closure", "catalog.tags",
]

IDENTITY_COLUMNS = [
    ("app.people", "id"),
    ("app.card_aliases", "id"),
    ("app.decklists", "id"),
    ("app.deck_cards", "id"),
    ("app.inventory", "id"),
    ("app.price_alert_log", "id"),
    ("catalog.cards", "card_id"),
    ("catalog.external_ids", "external_id_id"),
    ("catalog.illustrations", "illustration_id"),
    ("catalog.images", "image_id"),
    ("catalog.printings", "printing_id"),
    ("catalog.product_price_history", "id"),
    ("catalog.sets", "set_id"),
    ("catalog.tags", "tag_id"),
]


def clean(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def norm(value):
    value = clean(value)
    return value.casefold() if value else None


def timestamp(value, field):
    if value is None or not str(value).strip():
        raise RuntimeError(f"Missing required timestamp: {field}")
    try:
        result = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(f"Invalid timestamp in {field}: {value!r}") from exc
    # SQLite CURRENT_TIMESTAMP values have no offset and represent UTC.
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result


def json_value(value, field, expected_type, default=None):
    if value is None:
        result = default
    else:
        try:
            result = json.loads(value) if isinstance(value, str) else value
        except (ValueError, TypeError) as exc:
            raise RuntimeError(f"Invalid JSON in {field}") from exc

    if result is None and default is not None:
        result = default

    if expected_type is not None and not isinstance(result, expected_type):
        raise RuntimeError(f"Unexpected JSON type in {field}")

    return result


def rulings_cache_records(inv, printing_ids):
    """Yield provider-scoped PostgreSQL rows from the legacy SQLite rulings cache."""
    for row in sqlite_rows(
        inv,
        "SELECT scryfall_id, rulings_json, fetched_at "
        "FROM card_rulings_cache ORDER BY scryfall_id",
    ):
        sid = clean(row["scryfall_id"])
        if not sid:
            raise RuntimeError("Blank Scryfall ID in card_rulings_cache")

        yield (
            GAME,
            SCRYFALL,
            sid,
            printing_ids.get(sid),
            Jsonb(json_value(
                row["rulings_json"], "card_rulings_cache.rulings_json", list, []
            )),
            timestamp(
                row["fetched_at"], "card_rulings_cache.fetched_at"
            ),
        )


def source_db(filename):
    path = SOURCE / filename
    if not path.is_file():
        raise FileNotFoundError(path)
    if os.access(path, os.W_OK):
        raise RuntimeError(f"Source database is not read-only: {path}")

    db = sqlite3.connect(
        path.resolve().as_uri() + "?mode=ro&immutable=1",
        uri=True,
    )
    db.row_factory = sqlite3.Row
    return db


def sqlite_rows(db, query, params=()):
    cursor = db.execute(query, params)
    while True:
        batch = cursor.fetchmany(2000)
        if not batch:
            break
        yield from batch


def put(pg, table, columns, rows, expected, batch_size=2000):
    expected.setdefault(table, 0)
    identifier = sql.Identifier(*table.split("."))
    column_sql = sql.SQL(", ").join(sql.Identifier(c) for c in columns)
    placeholders = sql.SQL(", ").join(sql.Placeholder() for _ in columns)
    statement = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        identifier, column_sql, placeholders
    )

    inserted = 0
    with pg.cursor() as cur:
        iterator = iter(rows)
        while True:
            batch = list(islice(iterator, batch_size))
            if not batch:
                break
            cur.executemany(statement, batch)
            inserted += len(batch)

    expected[table] += inserted
    return inserted


def pg_count(pg, table):
    return pg.execute(
        sql.SQL("SELECT COUNT(*) FROM {}").format(
            sql.Identifier(*table.split("."))
        )
    ).fetchone()[0]


def validate_sources(inv, art):
    for name, db in (
        ("mtg_inventory.db", inv),
        ("mtg_art_hashes.db", art),
    ):
        result = db.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"{name} integrity check failed: {result}")

        violations = db.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(
                f"{name} has {len(violations)} foreign-key violations"
            )

    required = {
        "mtg_inventory.db": [
            "app_migrations", "app_state", "card_aliases", "card_metadata",
            "card_rulings_cache",
            "card_price_mapping", "deck_cards", "decklists", "inventory",
            "login_attempts", "people", "price_alert_log", "price_history",
            "scan_log", "users",
        ],
        "mtg_art_hashes.db": [
            "art_tag_assignments", "card_art", "card_art_faces",
            "oracle_tag_assignments", "scryfall_tag_aliases",
            "scryfall_tag_closure", "scryfall_tags",
        ],
    }

    for db, filename in ((inv, "mtg_inventory.db"), (art, "mtg_art_hashes.db")):
        existing = {
            row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        missing = set(required[filename]) - existing
        if missing:
            raise RuntimeError(f"{filename} missing tables: {sorted(missing)}")

    checks = [
        (inv, "inventory has invalid required fields", """
            SELECT COUNT(*) FROM inventory
            WHERE name IS NULL OR trim(name)=''
               OR owner IS NULL OR trim(owner)=''
               OR finish IS NULL OR trim(finish)=''
               OR location IS NULL OR trim(location)=''
               OR price IS NULL OR quantity IS NULL OR quantity<=0
               OR scan_time IS NULL
        """),
        (inv, "inventory references missing decks", """
            SELECT COUNT(*) FROM inventory i
            LEFT JOIN decklists d ON d.id=i.deck_id
            WHERE i.deck_id IS NOT NULL AND d.id IS NULL
        """),
        (inv, "decklists have invalid required fields", """
            SELECT COUNT(*) FROM decklists
            WHERE name IS NULL OR trim(name)=''
               OR source IS NULL OR trim(source)=''
               OR created_at IS NULL OR updated_at IS NULL
        """),
        (inv, "deck cards have invalid required fields", """
            SELECT COUNT(*) FROM deck_cards
            WHERE name IS NULL OR trim(name)=''
               OR quantity IS NULL OR quantity<=0
        """),
        (inv, "people names collide after normalization", """
            SELECT COUNT(*) FROM (
                SELECT lower(trim(name)) FROM people
                GROUP BY lower(trim(name)) HAVING COUNT(*)>1
            )
        """),
        (inv, "usernames collide after normalization", """
            SELECT COUNT(*) FROM (
                SELECT lower(trim(username)) FROM users
                GROUP BY lower(trim(username)) HAVING COUNT(*)>1
            )
        """),
        (inv, "invalid deck tags JSON", """
            SELECT COUNT(*) FROM decklists
            WHERE tags_json IS NULL OR NOT json_valid(tags_json)
        """),
        (inv, "invalid deck-card role tags JSON", """
            SELECT COUNT(*) FROM deck_cards
            WHERE role_tags_json IS NULL OR NOT json_valid(role_tags_json)
        """),
        (inv, "invalid price mapping finishes JSON", """
            SELECT COUNT(*) FROM card_price_mapping
            WHERE finishes_json IS NOT NULL AND NOT json_valid(finishes_json)
        """),
        (inv, "invalid card metadata JSON", """
            SELECT COUNT(*) FROM card_metadata
            WHERE (colors_json IS NOT NULL AND NOT json_valid(colors_json))
               OR (color_identity_json IS NOT NULL AND NOT json_valid(color_identity_json))
               OR (legalities_json IS NOT NULL AND NOT json_valid(legalities_json))
        """),
        (art, "invalid tag parent/child JSON", """
            SELECT COUNT(*) FROM scryfall_tags
            WHERE parent_ids_json IS NULL OR NOT json_valid(parent_ids_json)
               OR child_ids_json IS NULL OR NOT json_valid(child_ids_json)
        """),
        (art, "duplicate normalized tag aliases", """
            SELECT COUNT(*) FROM (
                SELECT tag_id, lower(trim(alias))
                FROM scryfall_tag_aliases
                GROUP BY tag_id, lower(trim(alias))
                HAVING COUNT(*)>1
            )
        """),
        (art, "artwork assignments have missing tags", """
            SELECT COUNT(*) FROM art_tag_assignments a
            LEFT JOIN scryfall_tags t
              ON t.id=a.tag_id AND t.tag_type='art'
            WHERE t.id IS NULL
        """),
        (art, "oracle assignments have missing tags", """
            SELECT COUNT(*) FROM oracle_tag_assignments a
            LEFT JOIN scryfall_tags t
              ON t.id=a.tag_id AND t.tag_type='oracle'
            WHERE t.id IS NULL
        """),
        (art, "artwork faces have missing parent printings", """
            SELECT COUNT(*) FROM card_art_faces f
            LEFT JOIN card_art a ON a.scryfall_id=f.scryfall_id
            WHERE a.scryfall_id IS NULL
        """),
        (art, "artwork images have conflicting pHashes", """
            SELECT COUNT(*) FROM (
                SELECT image_uri FROM card_art
                WHERE image_uri IS NOT NULL AND trim(image_uri)<>''
                  AND phash IS NOT NULL AND trim(phash)<>''
                GROUP BY image_uri
                HAVING COUNT(DISTINCT phash)>1
            )
        """),
    ]

    checks.append(
        (
            inv,
            "invalid cached card rulings",
            """
                SELECT COUNT(*) FROM card_rulings_cache
                WHERE scryfall_id IS NULL OR trim(scryfall_id)=''
                   OR rulings_json IS NULL
                   OR CASE
                        WHEN json_valid(rulings_json)
                        THEN json_type(rulings_json) <> 'array'
                        ELSE 1
                      END
                   OR fetched_at IS NULL OR trim(fetched_at)=''
            """,
        )
    )

    for db, label, query in checks:
        failures = db.execute(query).fetchone()[0]
        if failures:
            raise RuntimeError(f"{label}: {failures:,} rows")


def reset_sequences(pg):
    for table, column in IDENTITY_COLUMNS:
        sequence = pg.execute(
            "SELECT pg_get_serial_sequence(%s, %s)",
            (table, column),
        ).fetchone()[0]
        if not sequence:
            continue

        maximum = pg.execute(
            sql.SQL("SELECT MAX({}) FROM {}").format(
                sql.Identifier(column),
                sql.Identifier(*table.split(".")),
            )
        ).fetchone()[0]

        pg.execute(
            "SELECT setval(%s::regclass, %s, %s)",
            (sequence, maximum if maximum is not None else 1, maximum is not None),
        )


def migrate(pg, inv, art):
    expected = {}
    print("Reading source rows and building identity maps...")

    art_rows = [dict(r) for r in art.execute(
        "SELECT * FROM card_art ORDER BY scryfall_id"
    )]
    face_rows = [dict(r) for r in art.execute(
        "SELECT * FROM card_art_faces ORDER BY scryfall_id, face_index"
    )]
    tag_rows = [dict(r) for r in art.execute(
        "SELECT * FROM scryfall_tags ORDER BY id"
    )]
    meta_rows = [dict(r) for r in inv.execute(
        "SELECT * FROM card_metadata ORDER BY scryfall_id"
    )]

    meta_by_printing = {
        clean(r["scryfall_id"]): r
        for r in meta_rows if clean(r["scryfall_id"])
    }

    faces_by_printing = defaultdict(list)
    for face in face_rows:
        faces_by_printing[clean(face["scryfall_id"])].append(face)

    # One set identity per normalized Scryfall set code.
    set_specs = {}
    for row in art_rows:
        code = clean(row["set_code"])
        if not code:
            continue
        key = code.casefold()
        spec = set_specs.setdefault(key, {"code": code, "names": set()})
        name = clean(row["set_name"])
        if name:
            spec["names"].add(name)

    set_keys = sorted(set_specs)
    set_ids = {key: i + 1 for i, key in enumerate(set_keys)}

    # A canonical card requires an Oracle ID and a known name.
    oracle_specs = {}
    for row in art_rows:
        sid = clean(row["scryfall_id"])
        meta = meta_by_printing.get(sid, {})
        oracle = clean(row.get("oracle_id")) or clean(meta.get("oracle_id"))
        name = clean(meta.get("name")) or clean(row.get("name"))
        if oracle and name and oracle not in oracle_specs:
            oracle_specs[oracle] = (name, meta, row)

    for meta in meta_rows:
        oracle = clean(meta.get("oracle_id"))
        name = clean(meta.get("name"))
        if oracle and name and oracle not in oracle_specs:
            oracle_specs[oracle] = (name, meta, {})

    oracle_keys = sorted(oracle_specs)
    card_ids = {key: i + 1 for i, key in enumerate(oracle_keys)}

    printing_keys = sorted(clean(r["scryfall_id"]) for r in art_rows)
    if None in printing_keys:
        raise RuntimeError("A card_art row has a blank Scryfall ID")
    printing_ids = {key: i + 1 for i, key in enumerate(printing_keys)}

    illustration_values = set()
    image_values = set()

    for row in art_rows:
        value = clean(row.get("illustration_id"))
        if value:
            illustration_values.add(value)
        value = clean(row.get("image_uri"))
        if value:
            image_values.add(value)

    for row in face_rows:
        value = clean(row.get("illustration_id"))
        if value:
            illustration_values.add(value)
        value = clean(row.get("image_uri"))
        if value:
            image_values.add(value)

    for row in art.execute(
        "SELECT DISTINCT illustration_id FROM art_tag_assignments"
    ):
        value = clean(row[0])
        if value:
            illustration_values.add(value)

    illustration_keys = sorted(illustration_values)
    illustration_ids = {
        key: i + 1 for i, key in enumerate(illustration_keys)
    }

    image_keys = sorted(image_values)
    image_ids = {key: i + 1 for i, key in enumerate(image_keys)}

    tag_keys = sorted(clean(r["id"]) for r in tag_rows)
    if None in tag_keys:
        raise RuntimeError("A Scryfall tag has a blank ID")
    tag_ids = {key: i + 1 for i, key in enumerate(tag_keys)}

    # Resolve each printing to its canonical card, where Oracle identity exists.
    card_by_printing = {}
    for row in art_rows:
        sid = clean(row["scryfall_id"])
        meta = meta_by_printing.get(sid, {})
        oracle = clean(row.get("oracle_id")) or clean(meta.get("oracle_id"))
        card_by_printing[sid] = card_ids.get(oracle)

    print(
        "Identity maps:",
        f"{len(card_ids):,} cards,",
        f"{len(printing_ids):,} printings,",
        f"{len(set_ids):,} sets,",
        f"{len(illustration_ids):,} illustrations,",
        f"{len(image_ids):,} images,",
        f"{len(tag_ids):,} tags",
    )

    # SETS
    put(pg, "catalog.sets",
        ["set_id", "game_id", "name", "set_code", "metadata"],
        (
            (
                set_ids[key], GAME,
                sorted(set_specs[key]["names"])[0]
                if set_specs[key]["names"] else set_specs[key]["code"],
                set_specs[key]["code"],
                Jsonb({
                    "source": "sqlite_backup",
                    "source_set_names": sorted(set_specs[key]["names"]),
                }),
            )
            for key in set_keys
        ), expected)

    # CANONICAL CARDS
    put(pg, "catalog.cards",
        ["card_id", "game_id", "name", "type_line", "rules_text", "metadata"],
        (
            (
                card_ids[oracle], GAME, info[0],
                clean(info[1].get("type_line")),
                clean(info[1].get("oracle_text")),
                Jsonb({
                    "source": "sqlite_backup",
                    "oracle_id": oracle,
                    "legacy_metadata": info[1] or {},
                }),
            )
            for oracle, info in sorted(oracle_specs.items())
        ), expected)

    # ILLUSTRATIONS
    put(pg, "catalog.illustrations",
        ["illustration_id", "game_id", "metadata"],
        (
            (
                illustration_ids[value], GAME,
                Jsonb({
                    "source": "sqlite_backup",
                    "external_illustration_id": value,
                }),
            )
            for value in illustration_keys
        ), expected)

    # IMAGES
    put(pg, "catalog.images",
        ["image_id", "game_id", "provider_id", "image_uri", "image_role", "metadata"],
        (
            (
                image_ids[uri], GAME, SCRYFALL, uri, "card_face",
                Jsonb({"source": "sqlite_backup"}),
            )
            for uri in image_keys
        ), expected)

    # PRINTINGS. Unknown finishes, language and release dates remain unknown.
    def printing_records():
        for row in art_rows:
            sid = clean(row["scryfall_id"])
            meta = meta_by_printing.get(sid, {})
            oracle = clean(row.get("oracle_id")) or clean(meta.get("oracle_id"))
            code = clean(row.get("set_code"))
            set_id = set_ids.get(code.casefold()) if code else None
            top_uri = clean(row.get("image_uri"))

            if not top_uri:
                for face in faces_by_printing.get(sid, []):
                    if int(face["face_index"]) == 0:
                        top_uri = clean(face.get("image_uri"))
                        break

            metadata = dict(row)
            metadata["migration_source"] = "sqlite_backup"

            yield (
                printing_ids[sid], GAME, card_ids.get(oracle), set_id,
                clean(row.get("collector_number")), None,
                clean(row.get("rarity")), None, [],
                image_ids.get(top_uri), None, None, Jsonb(metadata),
            )

    put(pg, "catalog.printings",
        [
            "printing_id", "game_id", "card_id", "set_id",
            "collector_number", "language_code", "rarity",
            "printing_variant", "available_finishes", "image_id",
            "released_at", "is_promo", "metadata",
        ], printing_records(), expected)

    # PROVIDER-SCOPED EXTERNAL IDENTITIES
    def external_records():
        next_id = 1

        for oracle, card_id in sorted(card_ids.items()):
            yield (
                next_id, GAME, SCRYFALL, "card", "oracle_id",
                oracle, card_id, None, None, None, Jsonb({}),
            )
            next_id += 1

        for sid, printing_id in sorted(printing_ids.items()):
            yield (
                next_id, GAME, SCRYFALL, "printing", "scryfall_id",
                sid, None, printing_id, None, None, Jsonb({}),
            )
            next_id += 1

        for key in set_keys:
            code = set_specs[key]["code"]
            yield (
                next_id, GAME, SCRYFALL, "set", "set_code",
                code, None, None, set_ids[key], None, Jsonb({}),
            )
            next_id += 1

        for value, illustration_id in sorted(illustration_ids.items()):
            yield (
                next_id, GAME, SCRYFALL, "illustration", "illustration_id",
                value, None, None, None, illustration_id, Jsonb({}),
            )
            next_id += 1

    put(pg, "catalog.external_ids",
        [
            "external_id_id", "game_id", "provider_id", "entity_type",
            "external_id_type", "external_id", "card_id", "printing_id",
            "set_id", "illustration_id", "metadata",
        ], external_records(), expected)

    # TAGS
    put(pg, "catalog.tags",
        [
            "tag_id", "game_id", "provider_id", "external_tag_id",
            "tag_type", "slug", "label", "description",
            "parent_ids", "child_ids", "metadata",
        ],
        (
            (
                tag_ids[clean(row["id"])], GAME, SCRYFALL,
                clean(row["id"]), row["tag_type"], row["slug"],
                row["label"], row["description"],
                Jsonb(json_value(row["parent_ids_json"], "tag parents", list, [])),
                Jsonb(json_value(row["child_ids_json"], "tag children", list, [])),
                Jsonb({"source": "sqlite_backup"}),
            )
            for row in tag_rows
        ), expected)

    # TAG ALIASES AND TRANSITIVE CLOSURE
    put(
        pg,
        "catalog.tag_aliases",
        ["tag_id", "game_id", "provider_id", "alias"],
        (
            (tag_ids[clean(r["tag_id"])], GAME, SCRYFALL, r["alias"])
            for r in art.execute(
                "SELECT tag_id, alias FROM scryfall_tag_aliases"
            )
        ),
        expected,
    )

    # Precompute tag types once; avoid scanning all tags per hierarchy row.
    tag_type_by_id = {
        clean(row["id"]): row["tag_type"] for row in tag_rows
    }

    put(
        pg,
        "catalog.tag_closure",
        [
            "game_id", "provider_id", "tag_type",
            "ancestor_tag_id", "descendant_tag_id", "depth",
        ],
        (
            (
                GAME,
                SCRYFALL,
                tag_type_by_id[clean(r["ancestor_tag_id"])],
                tag_ids[clean(r["ancestor_tag_id"])],
                tag_ids[clean(r["descendant_tag_id"])],
                int(r["depth"]),
            )
            for r in art.execute(
                "SELECT ancestor_tag_id, descendant_tag_id, depth "
                "FROM scryfall_tag_closure"
            )
        ),
        expected,
    )

    # PRINTING FACES. The top-level artwork row supplies face zero only when
    # the legacy face table did not contain one.
    def face_records():
        for art_row in art_rows:
            sid = clean(art_row["scryfall_id"])
            meta = meta_by_printing.get(sid, {})
            faces = faces_by_printing.get(sid, [])
            has_zero = any(int(f["face_index"]) == 0 for f in faces)

            source_faces = list(faces)
            if not has_zero:
                source_faces.append({
                    "scryfall_id": sid,
                    "face_index": 0,
                    "oracle_id": art_row.get("oracle_id"),
                    "illustration_id": art_row.get("illustration_id"),
                    "image_uri": art_row.get("image_uri"),
                    "_synthetic": True,
                })

            for face in sorted(source_faces, key=lambda f: int(f["face_index"])):
                index = int(face["face_index"])
                uri = clean(face.get("image_uri"))
                illustration = clean(face.get("illustration_id"))

                if index == 0:
                    uri = uri or clean(art_row.get("image_uri"))
                    illustration = illustration or clean(
                        art_row.get("illustration_id")
                    )

                data = dict(face)
                data["migration_source"] = "sqlite_backup"

                yield (
                    printing_ids[sid], GAME, index,
                    clean(art_row.get("name")) if index == 0 else None,
                    clean(meta.get("type_line")) if index == 0 else None,
                    clean(meta.get("oracle_text")) if index == 0 else None,
                    None, image_ids.get(uri),
                    illustration_ids.get(illustration),
                    Jsonb(data),
                )

    put(pg, "catalog.printing_faces",
        [
            "printing_id", "game_id", "face_index", "name", "type_line",
            "rules_text", "flavor_text", "image_id", "illustration_id",
            "metadata",
        ], face_records(), expected)

    # PRESERVE EXISTING pHASH VALUES. No image downloads or recalculation.
    hashes_by_uri = {}
    for row in art_rows:
        uri = clean(row.get("image_uri"))
        value = clean(row.get("phash"))
        if uri and value:
            prior = hashes_by_uri.setdefault(uri, value)
            if prior != value:
                raise RuntimeError(f"Conflicting pHash for image URI: {uri}")

    put(pg, "catalog.image_hashes",
        [
            "image_id", "game_id", "hash_algorithm",
            "hash_version", "hash_value", "metadata",
        ],
        (
            (
                image_ids[uri], GAME, "phash", "1", value,
                Jsonb({
                    "source": "mtg_art_hashes.db",
                    "original_hash_timestamp_available": False,
                }),
            )
            for uri, value in sorted(hashes_by_uri.items())
        ), expected)

    # LEGACY PER-PRINTING METADATA
    def provider_metadata_records():
        for row in meta_rows:
            sid = clean(row["scryfall_id"])
            colors = json_value(row.get("colors_json"), "colors_json", list)
            identity = json_value(
                row.get("color_identity_json"), "color_identity_json", list
            )
            legalities = json_value(
                row.get("legalities_json"), "legalities_json", dict
            )

            yield (
                GAME, SCRYFALL, sid, printing_ids.get(sid),
                clean(row.get("name")), clean(row.get("type_line")),
                Jsonb(colors) if colors is not None else None,
                clean(row.get("colors_text")),
                Jsonb(identity) if identity is not None else None,
                float(row["mana_value"]) if row.get("mana_value") is not None else None,
                clean(row.get("mana_cost")), clean(row.get("oracle_text")),
                clean(row.get("oracle_id")),
                Jsonb(legalities) if legalities is not None else None,
                timestamp(row.get("updated_at"), "card_metadata.updated_at"),
            )

    put(pg, "catalog.provider_printing_metadata",
        [
            "game_id", "provider_id", "external_printing_id", "printing_id",
            "name", "type_line", "colors", "colors_text", "color_identity",
            "mana_value", "mana_cost", "oracle_text", "external_oracle_id",
            "legalities", "updated_at",
        ], provider_metadata_records(), expected)

    # CACHED SCRYFALL RULINGS
    put(
        pg,
        "catalog.printing_rulings_cache",
        [
            "game_id", "provider_id", "external_printing_id", "printing_id",
            "rulings", "fetched_at",
        ],
        rulings_cache_records(inv, printing_ids),
        expected,
    )

    # PRODUCT MAPPINGS AND HISTORICAL PRICES
    # Explicit price mappings take precedence. Fill missing TCGCSV product IDs
    # from the Scryfall artwork catalog, retaining their source build timestamp.
    def product_mapping_records():
        explicit_by_sid = {}

        for row in sqlite_rows(inv, "SELECT * FROM card_price_mapping"):
            sid = clean(row["scryfall_id"])
            if not sid:
                raise RuntimeError("Blank Scryfall ID in card_price_mapping")
            explicit_by_sid[sid] = dict(row)

        art_by_sid = {
            clean(row["scryfall_id"]): row
            for row in art_rows
            if clean(row["scryfall_id"])
        }

        art_product_sids = {
            sid for sid, row in art_by_sid.items()
            if clean(row.get("tcgplayer_id"))
        }

        explicit_product_count = 0
        artwork_fallback_count = 0

        for sid in sorted(set(explicit_by_sid) | art_product_sids):
            explicit = explicit_by_sid.get(sid)
            artwork = art_by_sid.get(sid)

            explicit_product = (
                clean(explicit.get("tcgplayer_id")) if explicit else None
            )
            artwork_product = (
                clean(artwork.get("tcgplayer_id")) if artwork else None
            )

            if (
                explicit_product and artwork_product
                and explicit_product != artwork_product
            ):
                raise RuntimeError(
                    f"Conflicting TCGplayer IDs for Scryfall printing {sid}"
                )

            product = explicit_product or artwork_product

            if explicit:
                finishes = json_value(
                    explicit.get("finishes_json"),
                    "card_price_mapping.finishes_json",
                    list,
                    [],
                )
                card_name = (
                    clean(explicit.get("card_name"))
                    or clean(artwork.get("name") if artwork else None)
                )
                set_code = (
                    clean(explicit.get("set_code"))
                    or clean(artwork.get("set_code") if artwork else None)
                )
                collector_number = (
                    clean(explicit.get("collector_number"))
                    or clean(artwork.get("collector_number") if artwork else None)
                )
            else:
                finishes = []
                card_name = clean(artwork.get("name"))
                set_code = clean(artwork.get("set_code"))
                collector_number = clean(artwork.get("collector_number"))

            if explicit_product:
                resolved_at = timestamp(
                    explicit["resolved_at"],
                    "card_price_mapping.resolved_at",
                )
                explicit_product_count += 1
            elif artwork_product:
                resolved_at = timestamp(
                    artwork.get("last_seen_build"),
                    "card_art.last_seen_build",
                )
                artwork_fallback_count += 1
            else:
                resolved_at = timestamp(
                    explicit["resolved_at"],
                    "card_price_mapping.resolved_at",
                )

            yield (
                GAME, SCRYFALL, sid, TCGCSV,
                printing_ids.get(sid), product, Jsonb(finishes),
                card_name, set_code, collector_number, resolved_at,
            )

        print(
            "TCGCSV product mapping sources:",
            f"{explicit_product_count:,} explicit IDs,",
            f"{artwork_fallback_count:,} artwork fallback IDs",
        )

    put(pg, "catalog.printing_product_mappings",
        [
            "game_id", "source_provider_id", "source_external_printing_id",
            "target_provider_id", "printing_id", "target_external_product_id",
            "finishes", "card_name", "set_code", "collector_number", "resolved_at",
        ], product_mapping_records(), expected)

    def price_history_records():
        for row in sqlite_rows(inv, "SELECT * FROM price_history ORDER BY id"):
            yield (
                row["id"], GAME, TCGCSV, str(row["tcgplayer_id"]),
                timestamp(row["captured_at"], "price_history.captured_at"),
                str(row["source_updated"]),
                row["normal_price"], row["foil_price"],
            )

    put(pg, "catalog.product_price_history",
        [
            "id", "game_id", "provider_id", "external_product_id",
            "captured_at", "source_updated", "normal_price", "foil_price",
        ], price_history_records(), expected)

    # APPLICATION MIGRATION STATE AND PEOPLE
    put(pg, "app.app_migrations", ["name", "applied_at"],
        ((r["name"], r["applied_at"])
         for r in inv.execute("SELECT name, applied_at FROM app_migrations")),
        expected)

    put(pg, "app.app_state", ["key", "value"],
        ((r["key"], r["value"])
         for r in inv.execute("SELECT key, value FROM app_state")),
        expected)

    people_rows = [dict(r) for r in inv.execute("SELECT * FROM people ORDER BY id")]
    people_by_name = {
        norm(r["name"]): int(r["id"])
        for r in people_rows if norm(r["name"])
    }

    put(pg, "app.people", ["id", "name", "active", "created_at"],
        (
            (
                r["id"], r["name"], bool(r["active"]),
                timestamp(r["created_at"], "people.created_at"),
            )
            for r in people_rows
        ), expected)

    # USERS: password hashes are copied byte-for-byte; never printed.
    put(pg, "app.users",
        [
            "username", "password_hash", "is_admin", "role",
            "is_superadmin", "active", "created_at", "last_login_at",
            "person_id",
        ],
        (
            (
                r["username"], r["password_hash"], bool(r["is_admin"]),
                r["role"], bool(r["is_superadmin"]), bool(r["active"]),
                timestamp(r["created_at"], "users.created_at"),
                timestamp(r["last_login_at"], "users.last_login_at")
                    if r["last_login_at"] else None,
                r["person_id"],
            )
            for r in inv.execute("SELECT * FROM users")
        ), expected)

    # DECKLISTS
    put(pg, "app.decklists",
        [
            "id", "game_id", "name", "source", "source_url",
            "created_at", "updated_at", "tags_json", "person_id",
            "person_name", "cover_filename", "cover_scryfall_id",
            "cover_printing_id",
        ],
        (
            (
                r["id"], GAME, r["name"], r["source"], r["source_url"],
                timestamp(r["created_at"], "decklists.created_at"),
                timestamp(r["updated_at"], "decklists.updated_at"),
                Jsonb(json_value(r["tags_json"], "decklists.tags_json", list, [])),
                people_by_name.get(norm(r["person_name"])),
                r["person_name"], r["cover_filename"], r["cover_scryfall_id"],
                printing_ids.get(clean(r["cover_scryfall_id"])),
            )
            for r in inv.execute("SELECT * FROM decklists")
        ), expected)

    # DECK CARDS
    put(pg, "app.deck_cards",
        [
            "id", "deck_id", "game_id", "name", "quantity", "scryfall_id",
            "set_code", "collector_number", "category", "finish",
            "tcgplayer_id", "role_tags_json", "any_printing",
            "card_id", "printing_id",
        ],
        (
            (
                r["id"], r["deck_id"], GAME, r["name"], r["quantity"],
                r["scryfall_id"], r["set_code"], r["collector_number"],
                r["category"], r["finish"], r["tcgplayer_id"],
                Jsonb(json_value(
                    r["role_tags_json"], "deck_cards.role_tags_json", list, []
                )),
                bool(r["any_printing"]),
                card_by_printing.get(clean(r["scryfall_id"])),
                printing_ids.get(clean(r["scryfall_id"])),
            )
            for r in inv.execute("SELECT * FROM deck_cards")
        ), expected)

    # INVENTORY
    put(pg, "app.inventory",
        [
            "id", "game_id", "card_id", "printing_id", "scryfall_id",
            "name", "set_code", "rarity", "price", "scan_time",
            "quantity", "owner", "person_id", "finish",
            "collector_number", "tcgplayer_id", "location", "deck_id",
            "for_vending",
        ],
        (
            (
                r["id"], GAME,
                card_by_printing.get(clean(r["scryfall_id"])),
                printing_ids.get(clean(r["scryfall_id"])),
                r["scryfall_id"], r["name"], r["set_code"], r["rarity"],
                str(r["price"]),
                timestamp(r["scan_time"], "inventory.scan_time"),
                r["quantity"], r["owner"],
                people_by_name.get(norm(r["owner"])),
                r["finish"], r["collector_number"], r["tcgplayer_id"],
                r["location"], r["deck_id"], bool(r["for_vending"]),
            )
            for r in inv.execute("SELECT * FROM inventory")
        ), expected)

    # REMAINING APPLICATION TABLES
    put(pg, "app.login_attempts",
        ["key", "first_attempt", "attempts", "blocked_until"],
        (
            (r["key"], r["first_attempt"], r["attempts"], r["blocked_until"])
            for r in inv.execute("SELECT * FROM login_attempts")
        ), expected)

    put(pg, "app.card_aliases",
        [
            "id", "game_id", "provider_id", "external_printing_id",
            "printing_id", "alias", "created_at",
        ],
        (
            (
                r["id"], GAME, SCRYFALL, r["scryfall_id"],
                printing_ids.get(clean(r["scryfall_id"])), r["alias"],
                timestamp(r["created_at"], "card_aliases.created_at"),
            )
            for r in inv.execute("SELECT * FROM card_aliases")
        ), expected)

    put(pg, "app.scan_log",
        ["scan_id", "game_id", "row_id", "quantity", "created_at"],
        (
            (
                r["scan_id"], GAME, r["row_id"], r["quantity"],
                timestamp(r["created_at"], "scan_log.created_at"),
            )
            for r in inv.execute("SELECT * FROM scan_log")
        ), expected)

    put(pg, "app.price_alert_log",
        [
            "id", "game_id", "provider_id", "external_product_id",
            "finish", "alerted_at", "old_price", "new_price",
            "percent_change", "owned_quantity", "owned_value_gain",
        ],
        (
            (
                r["id"], GAME, TCGCSV, str(r["tcgplayer_id"]), r["finish"],
                timestamp(r["alerted_at"], "price_alert_log.alerted_at"),
                r["old_price"], r["new_price"], r["percent_change"],
                r["owned_quantity"], r["owned_value_gain"],
            )
            for r in inv.execute("SELECT * FROM price_alert_log")
        ), expected)

    # ART TAG ASSIGNMENTS
    put(pg, "catalog.art_tag_assignments",
        [
            "game_id", "provider_id", "tag_type", "tag_id",
            "illustration_id", "weight", "annotation",
        ],
        (
            (
                GAME, SCRYFALL, "art",
                tag_ids[clean(r["tag_id"])],
                illustration_ids[clean(r["illustration_id"])],
                r["weight"], r["annotation"],
            )
            for r in sqlite_rows(
                art, "SELECT * FROM art_tag_assignments"
            )
        ), expected)

    # ORACLE TAG ASSIGNMENTS
    put(pg, "catalog.oracle_tag_assignments",
        [
            "game_id", "provider_id", "tag_type", "tag_id",
            "external_oracle_id", "card_id", "weight", "annotation",
        ],
        (
            (
                GAME, SCRYFALL, "oracle",
                tag_ids[clean(r["tag_id"])],
                clean(r["oracle_id"]),
                card_ids.get(clean(r["oracle_id"])),
                r["weight"], r["annotation"],
            )
            for r in sqlite_rows(
                art, "SELECT * FROM oracle_tag_assignments"
            )
        ), expected)

    # Verify all imported table counts before commit/rollback.
    print("\n=== IMPORTED ROW COUNTS ===")
    for table in TARGET_TABLES:
        actual = pg_count(pg, table)
        wanted = expected.get(table, 0)
        print(f"{table}: {actual:,} (expected {wanted:,})")
        if actual != wanted:
            raise RuntimeError(
                f"Count mismatch in {table}: expected {wanted}, got {actual}"
            )

    # Increment the MTG catalog revision in the same transaction.
    updated = pg.execute("""
        UPDATE catalog.game_catalog_state
        SET revision = revision + 1, updated_at = now()
        WHERE game_id = 'mtg'
    """)
    if updated.rowcount != 1:
        raise RuntimeError("Could not update the MTG catalog revision")

    return expected


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply", action="store_true",
        help="Commit the migration. Default behavior is dry-run and rollback.",
    )
    args = parser.parse_args()

    dsn = os.environ.get("CARD_SCANNER_POSTGRES_DSN", "").strip()
    if not dsn:
        raise RuntimeError("CARD_SCANNER_POSTGRES_DSN is not set")

    parsed = urlparse(dsn)
    if (
        parsed.hostname, parsed.port, parsed.path.lstrip("/")
    ) != ("127.0.0.1", 5433, "cardscanner_dev"):
        raise RuntimeError(
            "Refusing to run: DSN must target 127.0.0.1:5433/cardscanner_dev"
        )

    inv = source_db("mtg_inventory.db")
    art = source_db("mtg_art_hashes.db")
    pg = None

    try:
        validate_sources(inv, art)
        print("SQLite source integrity and preflight: OK")

        pg = psycopg.connect(dsn)
        pg.execute("SET LOCAL TIME ZONE 'UTC'")
        pg.execute("SELECT pg_advisory_xact_lock(731942811)")
        target = pg.execute("""
            SELECT current_database(), current_user
        """).fetchone()

        if target[0] != "cardscanner_dev":
            raise RuntimeError(f"Unexpected database: {target[0]}")

        for provider in (SCRYFALL, TCGCSV):
            enabled = pg.execute("""
                SELECT is_enabled
                FROM catalog.game_providers
                WHERE game_id = %s AND provider_id = %s
            """, (GAME, provider)).fetchone()
            if not enabled or not enabled[0]:
                raise RuntimeError(f"Required MTG provider is not enabled: {provider}")

        print("PostgreSQL target:", target[0], "as", target[1])

        print("\nChecking destination tables are empty...")
        for table in TARGET_TABLES:
            count = pg_count(pg, table)
            if count:
                raise RuntimeError(
                    f"Refusing to mix data: {table} already contains {count:,} rows"
                )
        print("All migration destination tables are empty.")

        imported = migrate(pg, inv, art)

        if args.apply:
            print("\nResetting identity sequences...")
            reset_sequences(pg)
            pg.commit()
            print("\nMIGRATION COMMITTED.")
        else:
            pg.rollback()
            print("\nDRY RUN COMPLETE: transaction rolled back; no rows committed.")

        print(f"Tables checked: {len(imported)}")
        print("Source SQLite databases were never modified.")

    except Exception:
        if pg is not None:
            pg.rollback()
        raise
    finally:
        inv.close()
        art.close()
        if pg is not None:
            pg.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"\nMIGRATION STOPPED: {exc}", file=sys.stderr)
        sys.exit(1)
