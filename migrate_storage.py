"""Safely copy live CardScanner SQLite databases from DATA_DIR to DB_DIR.

Run only after stopping the web, updater, Scryfall, and backup services. Uses
SQLite's online backup API so committed WAL contents are included. Source files
are never modified or deleted, and existing destination databases are never
overwritten.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import tempfile
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB_DIR = Path(os.environ.get("DB_DIR", str(DATA_DIR)))
DATABASES = ("mtg_inventory.db", "mtg_art_hashes.db")


def integrity_check(path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f"Database is missing or empty: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        result = [str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()]
    finally:
        conn.close()
    if result != ["ok"]:
        raise RuntimeError(f"SQLite integrity check failed for {path}: {'; '.join(result[:10])}")


def copy_database(source: Path, destination: Path) -> None:
    """Create one verified SQLite backup without overwriting an existing file."""
    if not source.is_file():
        raise FileNotFoundError(f"Source database not found: {source}")
    if source.resolve() == destination.resolve():
        raise RuntimeError(f"Source and destination are already the same: {source}")
    if destination.exists():
        raise FileExistsError(
            f"Refusing to overwrite existing destination database: {destination}"
        )

    integrity_check(source)
    source_conn = sqlite3.connect(source, timeout=60)
    try:
        source_conn.execute("PRAGMA busy_timeout=10000")
        destination_conn = sqlite3.connect(destination)
        try:
            source_conn.backup(destination_conn, pages=256, sleep=0.10)
            destination_conn.commit()
        finally:
            destination_conn.close()
    finally:
        source_conn.close()

    try:
        integrity_check(destination)
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def copy_databases() -> list[Path]:
    """Stage and verify every database before publishing destination files."""
    DB_DIR.mkdir(parents=True, exist_ok=True)
    sources = [DATA_DIR / name for name in DATABASES]
    destinations = [DB_DIR / name for name in DATABASES]

    for source in sources:
        if not source.is_file():
            raise FileNotFoundError(
                f"Required source database is missing: {source}. "
                "Do not start the new deployment until both original databases are present."
            )
    for destination in destinations:
        if destination.exists():
            raise FileExistsError(
                f"Refusing to overwrite {destination}. Inspect it and choose an empty DB_DIR."
            )

    published: list[Path] = []
    try:
        with tempfile.TemporaryDirectory(prefix=".cardscanner-migration-", dir=str(DB_DIR)) as staging:
            staged = [Path(staging) / name for name in DATABASES]
            for source, temp_path in zip(sources, staged):
                print(f"[MIGRATE] Snapshotting {source} -> {temp_path}", flush=True)
                copy_database(source, temp_path)

            for temp_path, destination in zip(staged, destinations):
                # Destination was checked above; os.replace publishes the verified local copy.
                os.replace(temp_path, destination)
                published.append(destination)

        for destination in destinations:
            integrity_check(destination)
            print(f"[MIGRATE] Verified {destination} ({destination.stat().st_size:,} bytes)", flush=True)
        print("[MIGRATE] Complete. Original NFS databases were left untouched.", flush=True)
        return destinations
    except Exception:
        # If publishing the pair fails, remove only files created by this attempt.
        for destination in published:
            destination.unlink(missing_ok=True)
        raise


def verify_databases() -> list[Path]:
    paths = [DB_DIR / name for name in DATABASES]
    for path in paths:
        integrity_check(path)
        print(f"[VERIFY] OK: {path} ({path.stat().st_size:,} bytes)", flush=True)
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--copy", action="store_true", help="copy both databases; never overwrite")
    action.add_argument("--verify", action="store_true", help="check both destination databases")
    args = parser.parse_args()

    print(f"[MIGRATE] Source data directory: {DATA_DIR}", flush=True)
    print(f"[MIGRATE] Local database directory: {DB_DIR}", flush=True)
    if args.copy:
        copy_databases()
    else:
        verify_databases()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
