"""Shared SQLite schema for the Scryfall printing, image-hash, and Tagger catalog."""

from __future__ import annotations

import sqlite3
from pathlib import Path

_READY_PATHS: set[str] = set()


def ensure_art_catalog_schema(db_path: str | Path) -> None:
    """Create/migrate the catalog schema without deleting existing card hashes.

    The desktop scanner only reads the original card_art columns. New metadata,
    face mappings, and Tagger tables live alongside those columns so the existing
    authenticated desktop sync can distribute one consistent SQLite snapshot.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path.resolve())
    if key in _READY_PATHS and path.exists():
        return

    conn = sqlite3.connect(path, timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS card_art (
                scryfall_id TEXT PRIMARY KEY,
                name TEXT,
                set_code TEXT,
                set_name TEXT,
                rarity TEXT,
                tcgplayer_id TEXT,
                collector_number TEXT,
                phash TEXT,
                oracle_id TEXT,
                illustration_id TEXT,
                image_uri TEXT,
                last_seen_build TEXT
            )
            """
        )
        existing = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(card_art)").fetchall()
        }
        additions = {
            "set_name": "TEXT",
            "collector_number": "TEXT",
            "oracle_id": "TEXT",
            "illustration_id": "TEXT",
            "image_uri": "TEXT",
            "last_seen_build": "TEXT",
        }
        for name, ddl in additions.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE card_art ADD COLUMN {name} {ddl}")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS card_art_faces (
                scryfall_id TEXT NOT NULL,
                face_index INTEGER NOT NULL DEFAULT 0,
                oracle_id TEXT,
                illustration_id TEXT,
                image_uri TEXT,
                PRIMARY KEY (scryfall_id, face_index)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scryfall_tags (
                id TEXT PRIMARY KEY,
                slug TEXT NOT NULL,
                label TEXT NOT NULL,
                tag_type TEXT NOT NULL CHECK (tag_type IN ('art', 'oracle')),
                description TEXT,
                parent_ids_json TEXT NOT NULL DEFAULT '[]',
                child_ids_json TEXT NOT NULL DEFAULT '[]'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scryfall_tag_aliases (
                tag_id TEXT NOT NULL,
                alias TEXT NOT NULL COLLATE NOCASE,
                PRIMARY KEY (tag_id, alias)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scryfall_tag_closure (
                ancestor_tag_id TEXT NOT NULL,
                descendant_tag_id TEXT NOT NULL,
                depth INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (ancestor_tag_id, descendant_tag_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS art_tag_assignments (
                tag_id TEXT NOT NULL,
                illustration_id TEXT NOT NULL,
                weight TEXT,
                annotation TEXT,
                PRIMARY KEY (tag_id, illustration_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS oracle_tag_assignments (
                tag_id TEXT NOT NULL,
                oracle_id TEXT NOT NULL,
                weight TEXT,
                annotation TEXT,
                PRIMARY KEY (tag_id, oracle_id)
            )
            """
        )

        indexes = (
            "CREATE INDEX IF NOT EXISTS idx_card_art_name ON card_art(name COLLATE NOCASE)",
            "CREATE INDEX IF NOT EXISTS idx_card_art_set_number ON card_art(set_code COLLATE NOCASE, collector_number COLLATE NOCASE)",
            "CREATE INDEX IF NOT EXISTS idx_card_art_oracle ON card_art(oracle_id)",
            "CREATE INDEX IF NOT EXISTS idx_card_art_faces_illustration ON card_art_faces(illustration_id)",
            "CREATE INDEX IF NOT EXISTS idx_card_art_faces_oracle ON card_art_faces(oracle_id)",
            "CREATE INDEX IF NOT EXISTS idx_scryfall_tags_type_slug ON scryfall_tags(tag_type, slug COLLATE NOCASE)",
            "CREATE INDEX IF NOT EXISTS idx_scryfall_tag_aliases_alias ON scryfall_tag_aliases(alias COLLATE NOCASE)",
            "CREATE INDEX IF NOT EXISTS idx_tag_closure_descendant ON scryfall_tag_closure(descendant_tag_id)",
            "CREATE INDEX IF NOT EXISTS idx_art_tag_assignments_illustration ON art_tag_assignments(illustration_id)",
            "CREATE INDEX IF NOT EXISTS idx_oracle_tag_assignments_oracle ON oracle_tag_assignments(oracle_id)",
        )
        for statement in indexes:
            conn.execute(statement)
        conn.commit()
        _READY_PATHS.add(key)
    finally:
        conn.close()
