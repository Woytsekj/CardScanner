"""Daily Scryfall bulk-data synchronizer for the printing and Tagger catalog.

This service owns mtg_art_hashes.db. It keeps the scanner's original card_art
shape compatible, incrementally creates missing/changed pHashes, imports Scryfall
art/oracle tags, and commits tag snapshots atomically.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Iterator

import imagehash
import requests
from PIL import Image

from scryfall_catalog import ensure_art_catalog_schema

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB_ART = DATA_DIR / "mtg_art_hashes.db"
META_FILE = DATA_DIR / "scryfall_catalog_sync.json"
MANIFEST_URL = "https://api.scryfall.com/bulk-data"
USER_AGENT = os.environ.get(
    "SCRYFALL_USER_AGENT",
    "CardScanner/1.0 (https://github.com/Woytsekj/CardScanner; local MTG catalog)",
).strip()
TIMEOUT = max(10, int(os.environ.get("SCRYFALL_TIMEOUT", "30")))
BULK_TIMEOUT = max(60, int(os.environ.get("SCRYFALL_BULK_TIMEOUT", "300")))
IMAGE_TIMEOUT = max(5, int(os.environ.get("SCRYFALL_IMAGE_TIMEOUT", "30")))
CHECK_INTERVAL = max(3600, int(os.environ.get("SCRYFALL_CATALOG_CHECK_INTERVAL", "86400")))
IMAGE_WORKERS = max(1, min(8, int(os.environ.get("SCRYFALL_IMAGE_WORKERS", "4"))))
IMAGE_REQUEST_DELAY = max(0.05, float(os.environ.get("SCRYFALL_IMAGE_REQUEST_DELAY", "0.12")))
IMAGE_BATCH_SIZE = max(50, int(os.environ.get("SCRYFALL_IMAGE_BATCH_SIZE", "250")))

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
_rate_lock = threading.Lock()
_next_image_request = 0.0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect_art() -> sqlite3.Connection:
    ensure_art_catalog_schema(DB_ART)
    conn = sqlite3.connect(DB_ART, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    # Prefer WAL so the web app can keep reading while a bulk catalog update commits.
    # If an existing reader prevents switching journal mode during startup, keep the
    # current mode; the normal SQLite busy timeout still protects writes.
    try:
        current_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        if current_mode != "wal":
            conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError as exc:
        print(f"[scryfall] could not enable WAL; keeping the current SQLite journal mode: {exc}")
    return conn


def load_meta() -> dict[str, Any]:
    try:
        payload = json.loads(META_FILE.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_meta(meta: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp = META_FILE.with_suffix(".json.tmp")
    temp.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
    temp.replace(META_FILE)


def bulk_manifest() -> dict[str, dict[str, Any]]:
    response = _session.get(MANIFEST_URL, timeout=TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise RuntimeError("Scryfall bulk-data manifest did not contain a data list.")
    wanted = {"default_cards", "art_tags", "oracle_tags"}
    result = {
        str(item.get("type")): item
        for item in items
        if isinstance(item, dict) and item.get("type") in wanted
    }
    missing = sorted(wanted - set(result))
    if missing:
        raise RuntimeError("Scryfall bulk-data manifest is missing: " + ", ".join(missing))
    return result


def download_bulk(item: dict[str, Any], name: str) -> Path:
    uri = str(item.get("jsonl_download_uri") or item.get("download_uri") or "").strip()
    if not uri.startswith("https://"):
        raise RuntimeError(f"Scryfall {name} bulk item has no HTTPS download URI.")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    destination = DATA_DIR / f"scryfall_{name}.jsonl.gz"
    partial = destination.with_suffix(destination.suffix + ".part")
    try:
        with _session.get(uri, timeout=BULK_TIMEOUT, stream=True) as response:
            response.raise_for_status()
            with partial.open("wb") as output:
                for chunk in response.iter_content(1024 * 1024):
                    if chunk:
                        output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
        if partial.stat().st_size < 2:
            raise RuntimeError(f"Scryfall {name} bulk download was empty.")
        with partial.open("rb") as handle:
            magic = handle.read(2)
        if magic == b"\x1f\x8b":
            with gzip.open(partial, "rb") as check:
                check.read(1)
        partial.replace(destination)
        return destination
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def iter_bulk_records(path: Path) -> Iterator[dict[str, Any]]:
    """Stream current gzip JSONL exports; retain compatibility with legacy JSON arrays."""
    with path.open("rb") as raw:
        magic = raw.read(2)
    opener = gzip.open if magic == b"\x1f\x8b" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        first = ""
        while not first:
            char = handle.read(1)
            if not char:
                return
            if not char.isspace():
                first = char
        handle.seek(0)
        if first == "[":
            payload = json.load(handle)
            if not isinstance(payload, list):
                raise RuntimeError(f"Legacy Scryfall file {path.name} is not a JSON array.")
            for item in payload:
                if isinstance(item, dict):
                    yield item
            return
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"Invalid JSONL in {path.name} at line {line_number}: {exc}"
                ) from exc
            if isinstance(item, dict):
                yield item


def _image_for_card(card: dict[str, Any]) -> str | None:
    image_uris = card.get("image_uris")
    if isinstance(image_uris, dict) and image_uris.get("normal"):
        return str(image_uris["normal"])
    faces = card.get("card_faces")
    if isinstance(faces, list):
        for face in faces:
            if not isinstance(face, dict):
                continue
            images = face.get("image_uris")
            if isinstance(images, dict) and images.get("normal"):
                return str(images["normal"])
    return None


def _face_records(card: dict[str, Any]) -> list[tuple[int, str | None, str | None, str | None]]:
    faces = card.get("card_faces")
    records: list[tuple[int, str | None, str | None, str | None]] = []
    if isinstance(faces, list) and faces:
        for index, face in enumerate(faces):
            if not isinstance(face, dict):
                continue
            images = face.get("image_uris")
            image_uri = images.get("normal") if isinstance(images, dict) else None
            records.append((
                index,
                str(face.get("oracle_id") or card.get("oracle_id") or "") or None,
                str(face.get("illustration_id") or (card.get("illustration_id") if index == 0 else "") or "") or None,
                str(image_uri or "") or None,
            ))
    else:
        records.append((
            0,
            str(card.get("oracle_id") or "") or None,
            str(card.get("illustration_id") or "") or None,
            _image_for_card(card),
        ))
    return records


def import_printings(path: Path, build_updated_at: str) -> dict[str, int]:
    conn = connect_art()
    imported = 0
    images = 0
    try:
        conn.execute("BEGIN")
        for card in iter_bulk_records(path):
            sid = str(card.get("id") or "").strip()
            if not sid:
                continue
            image_uri = _image_for_card(card)
            old = conn.execute(
                "SELECT phash, image_uri FROM card_art WHERE scryfall_id=?",
                (sid,),
            ).fetchone()
            old_hash = old["phash"] if old else None
            old_uri = old["image_uri"] if old else None
            # A hash remains valid only when the exact image resource is unchanged.
            # Legacy art catalogs predate image_uri. Preserve their valid existing
            # pHashes on the first metadata migration instead of needlessly rehashing
            # every printing; subsequent URL changes still invalidate the hash.
            phash = old_hash if old_hash and image_uri and (old_uri is None or old_uri == image_uri) else None
            if image_uri:
                images += 1
            conn.execute(
                """
                INSERT INTO card_art (
                    scryfall_id,name,set_code,set_name,rarity,tcgplayer_id,
                    collector_number,phash,oracle_id,illustration_id,image_uri,last_seen_build
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(scryfall_id) DO UPDATE SET
                    name=excluded.name,
                    set_code=excluded.set_code,
                    set_name=excluded.set_name,
                    rarity=excluded.rarity,
                    tcgplayer_id=excluded.tcgplayer_id,
                    collector_number=excluded.collector_number,
                    phash=CASE
                        WHEN card_art.image_uri IS NULL AND COALESCE(card_art.phash,'')<>'' THEN card_art.phash
                        WHEN card_art.image_uri IS excluded.image_uri THEN card_art.phash
                        ELSE NULL
                    END,
                    oracle_id=excluded.oracle_id,
                    illustration_id=excluded.illustration_id,
                    image_uri=excluded.image_uri,
                    last_seen_build=excluded.last_seen_build
                """,
                (
                    sid,
                    card.get("name"),
                    card.get("set"),
                    card.get("set_name"),
                    card.get("rarity"),
                    str(card.get("tcgplayer_id")) if card.get("tcgplayer_id") is not None else None,
                    str(card.get("collector_number") or ""),
                    phash,
                    card.get("oracle_id"),
                    card.get("illustration_id") or next(
                        (f[2] for f in _face_records(card) if f[2]), None
                    ),
                    image_uri,
                    build_updated_at,
                ),
            )
            conn.execute("DELETE FROM card_art_faces WHERE scryfall_id=?", (sid,))
            conn.executemany(
                """
                INSERT INTO card_art_faces
                    (scryfall_id,face_index,oracle_id,illustration_id,image_uri)
                VALUES (?,?,?,?,?)
                """,
                [(sid, *face) for face in _face_records(card)],
            )
            imported += 1
            if imported % 1000 == 0:
                print(f"[scryfall] imported {imported:,} printings...")
        if imported == 0:
            raise RuntimeError("Scryfall default_cards export contained no card records.")
        conn.commit()
        return {"printings": imported, "with_images": images}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _throttle_image_requests() -> None:
    global _next_image_request
    with _rate_lock:
        now = time.monotonic()
        if now < _next_image_request:
            time.sleep(_next_image_request - now)
        _next_image_request = time.monotonic() + IMAGE_REQUEST_DELAY


def hash_image(url: str) -> tuple[str, str | None, str | None]:
    """Download and pHash one Scryfall card-face image with bounded retries."""
    for attempt in range(3):
        try:
            _throttle_image_requests()
            response = requests.get(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "image/*"},
                timeout=IMAGE_TIMEOUT,
            )
            response.raise_for_status()
            with Image.open(BytesIO(response.content)) as source:
                image = source.convert("RGB")
                return url, str(imagehash.phash(image)), None
        except Exception as exc:
            if attempt == 2:
                return url, None, str(exc)
            time.sleep(0.5 * (attempt + 1))
    return url, None, "image hashing failed"


def fill_missing_hashes(limit: int | None = None) -> dict[str, int]:
    conn = connect_art()
    try:
        query = """
            SELECT scryfall_id,image_uri
            FROM card_art
            WHERE COALESCE(TRIM(phash),'')=''
              AND COALESCE(TRIM(image_uri),'')<>''
            ORDER BY set_code COLLATE NOCASE, collector_number COLLATE NOCASE
        """
        rows = conn.execute(query).fetchall()
    finally:
        conn.close()
    if limit is not None:
        rows = rows[:max(0, int(limit))]
    pending = [(str(row["scryfall_id"]), str(row["image_uri"])) for row in rows]
    if not pending:
        return {"attempted": 0, "updated": 0, "failed": 0}
    print(
        f"[scryfall] generating pHashes for {len(pending):,} missing/changed "
        f"printing images with {IMAGE_WORKERS} workers..."
    )
    updated = 0
    failed = 0
    conn = connect_art()
    try:
        with ThreadPoolExecutor(max_workers=IMAGE_WORKERS) as pool:
            jobs = {pool.submit(hash_image, uri): sid for sid, uri in pending}
            batch: list[tuple[str, str, str]] = []
            for future in as_completed(jobs):
                sid = jobs[future]
                try:
                    url, phash, error = future.result()
                except Exception as exc:
                    url, phash, error = "", None, str(exc)
                if phash:
                    batch.append((phash, sid, url))
                    updated += 1
                else:
                    failed += 1
                    if failed <= 20:
                        print(f"[scryfall] image hash failed for {sid}: {error}")
                if len(batch) >= IMAGE_BATCH_SIZE:
                    conn.executemany(
                        "UPDATE card_art SET phash=? WHERE scryfall_id=? AND image_uri=?",
                        batch,
                    )
                    conn.commit()
                    batch.clear()
                done = updated + failed
                if done % 500 == 0:
                    print(f"[scryfall] image hashes {done:,}/{len(pending):,} (updated {updated:,}, failed {failed:,})")
            if batch:
                conn.executemany(
                    "UPDATE card_art SET phash=? WHERE scryfall_id=? AND image_uri=?",
                    batch,
                )
                conn.commit()
    finally:
        conn.close()
    return {"attempted": len(pending), "updated": updated, "failed": failed}


def import_tag_snapshots(art_path: Path, oracle_path: Path, versions: dict[str, str]) -> dict[str, int]:
    """Replace both Tagger datasets in one transaction; never expose a partial tag set."""
    conn = connect_art()
    try:
        conn.execute("BEGIN")
        for table, ddl in (
            ("tag_stage", "CREATE TEMP TABLE tag_stage (id TEXT PRIMARY KEY, slug TEXT NOT NULL, label TEXT NOT NULL, tag_type TEXT NOT NULL, description TEXT, parent_ids_json TEXT NOT NULL, child_ids_json TEXT NOT NULL)"),
            ("alias_stage", "CREATE TEMP TABLE alias_stage (tag_id TEXT NOT NULL, alias TEXT NOT NULL COLLATE NOCASE, PRIMARY KEY(tag_id,alias))"),
            ("closure_stage", "CREATE TEMP TABLE closure_stage (ancestor_tag_id TEXT NOT NULL, descendant_tag_id TEXT NOT NULL, depth INTEGER NOT NULL, PRIMARY KEY(ancestor_tag_id,descendant_tag_id))"),
            ("art_assignment_stage", "CREATE TEMP TABLE art_assignment_stage (tag_id TEXT NOT NULL, illustration_id TEXT NOT NULL, weight TEXT, annotation TEXT, PRIMARY KEY(tag_id,illustration_id))"),
            ("oracle_assignment_stage", "CREATE TEMP TABLE oracle_assignment_stage (tag_id TEXT NOT NULL, oracle_id TEXT NOT NULL, weight TEXT, annotation TEXT, PRIMARY KEY(tag_id,oracle_id))"),
        ):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
            conn.execute(ddl)

        parents: dict[str, list[str]] = {}
        tag_count = 0
        alias_count = 0
        art_count = 0
        oracle_count = 0

        for kind, path in (("art", art_path), ("oracle", oracle_path)):
            for tag in iter_bulk_records(path):
                tag_id = str(tag.get("id") or "").strip()
                slug = str(tag.get("slug") or "").strip()
                label = str(tag.get("label") or "").strip()
                if not tag_id or not slug or not label:
                    continue
                parents[tag_id] = [
                    str(value).strip()
                    for value in (tag.get("parent_ids") or [])
                    if str(value).strip()
                ]
                conn.execute(
                    """
                    INSERT OR REPLACE INTO tag_stage
                    (id,slug,label,tag_type,description,parent_ids_json,child_ids_json)
                    VALUES(?,?,?,?,?,?,?)
                    """,
                    (
                        tag_id, slug, label, kind, tag.get("description"),
                        json.dumps(parents[tag_id], separators=(",", ":")),
                        json.dumps(tag.get("child_ids") or [], separators=(",", ":")),
                    ),
                )
                aliases = tag.get("aliases") if isinstance(tag.get("aliases"), list) else []
                for alias in aliases:
                    value = str(alias).strip()
                    if value:
                        conn.execute(
                            "INSERT OR IGNORE INTO alias_stage(tag_id,alias) VALUES(?,?)",
                            (tag_id, value),
                        )
                        alias_count += 1
                for tagging in tag.get("taggings") or []:
                    if not isinstance(tagging, dict):
                        continue
                    weight = str(tagging.get("weight") or "median")
                    annotation = tagging.get("annotation")
                    if kind == "art":
                        entity_id = str(tagging.get("illustration_id") or "").strip()
                        if entity_id:
                            conn.execute(
                                "INSERT OR IGNORE INTO art_assignment_stage(tag_id,illustration_id,weight,annotation) VALUES(?,?,?,?)",
                                (tag_id, entity_id, weight, annotation),
                            )
                            art_count += 1
                    else:
                        entity_id = str(tagging.get("oracle_id") or "").strip()
                        if entity_id:
                            conn.execute(
                                "INSERT OR IGNORE INTO oracle_assignment_stage(tag_id,oracle_id,weight,annotation) VALUES(?,?,?,?)",
                                (tag_id, entity_id, weight, annotation),
                            )
                            oracle_count += 1
                tag_count += 1
            if kind == "art" and art_count == 0:
                raise RuntimeError("Scryfall art_tags export contained no illustration taggings.")
            if kind == "oracle" and oracle_count == 0:
                raise RuntimeError("Scryfall oracle_tags export contained no Oracle taggings.")

        # The hierarchy is kept as a transitive closure for descendants of a parent.
        for descendant, parent_ids in parents.items():
            visited: set[str] = set()
            frontier = [(parent, 1) for parent in parent_ids]
            while frontier:
                ancestor, depth = frontier.pop()
                if ancestor == descendant or ancestor in visited:
                    continue
                visited.add(ancestor)
                conn.execute(
                    "INSERT OR IGNORE INTO closure_stage(ancestor_tag_id,descendant_tag_id,depth) VALUES(?,?,?)",
                    (ancestor, descendant, depth),
                )
                frontier.extend((parent, depth + 1) for parent in parents.get(ancestor, []))

        for target, stage in (
            ("art_tag_assignments", "art_assignment_stage"),
            ("oracle_tag_assignments", "oracle_assignment_stage"),
            ("scryfall_tag_aliases", "alias_stage"),
            ("scryfall_tag_closure", "closure_stage"),
            ("scryfall_tags", "tag_stage"),
        ):
            conn.execute(f"DELETE FROM {target}")
            columns = {
                "art_tag_assignments": "tag_id,illustration_id,weight,annotation",
                "oracle_tag_assignments": "tag_id,oracle_id,weight,annotation",
                "scryfall_tag_aliases": "tag_id,alias",
                "scryfall_tag_closure": "ancestor_tag_id,descendant_tag_id,depth",
                "scryfall_tags": "id,slug,label,tag_type,description,parent_ids_json,child_ids_json",
            }[target]
            conn.execute(f"INSERT INTO {target} ({columns}) SELECT {columns} FROM {stage}")
        conn.commit()
        return {
            "tags": tag_count,
            "aliases": alias_count,
            "art_assignments": art_count,
            "oracle_assignments": oracle_count,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def sync_once(*, force: bool = False, skip_images: bool = False, image_limit: int | None = None) -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    manifest = bulk_manifest()
    old_meta = load_meta()
    prior_sources = old_meta.get("sources") if isinstance(old_meta.get("sources"), dict) else {}
    current_sources = {
        key: str(value.get("updated_at") or "")
        for key, value in manifest.items()
    }
    conn = connect_art()
    try:
        card_count = int(conn.execute("SELECT COUNT(*) FROM card_art").fetchone()[0])
        tag_count = int(conn.execute("SELECT COUNT(*) FROM scryfall_tags").fetchone()[0])
    finally:
        conn.close()

    default_updated = current_sources["default_cards"]
    if force or not card_count or prior_sources.get("default_cards") != default_updated:
        print(f"[scryfall] downloading default_cards ({default_updated})...")
        printings_path = download_bulk(manifest["default_cards"], "default_cards")
        printings = import_printings(printings_path, default_updated)
    else:
        printings = {"printings": card_count, "with_images": 0}
        print("[scryfall] default_cards unchanged; retaining existing printing metadata.")

    tags_changed = (
        force
        or not tag_count
        or prior_sources.get("art_tags") != current_sources["art_tags"]
        or prior_sources.get("oracle_tags") != current_sources["oracle_tags"]
    )
    tag_stats = {"tags": tag_count, "aliases": 0, "art_assignments": 0, "oracle_assignments": 0}
    if tags_changed:
        print("[scryfall] downloading art_tags and oracle_tags...")
        art_path = download_bulk(manifest["art_tags"], "art_tags")
        oracle_path = download_bulk(manifest["oracle_tags"], "oracle_tags")
        tag_stats = import_tag_snapshots(
            art_path, oracle_path,
            {"art_tags": current_sources["art_tags"], "oracle_tags": current_sources["oracle_tags"]},
        )
    else:
        print("[scryfall] art_tags and oracle_tags unchanged; retaining current tag index.")

    image_stats = {"attempted": 0, "updated": 0, "failed": 0}
    if not skip_images:
        image_stats = fill_missing_hashes(limit=image_limit)

    conn = connect_art()
    try:
        total_rows = int(conn.execute("SELECT COUNT(*) FROM card_art").fetchone()[0])
        total_hashes = int(conn.execute(
            "SELECT COUNT(*) FROM card_art WHERE COALESCE(TRIM(phash),'')<>''"
        ).fetchone()[0])
        art_tags = int(conn.execute("SELECT COUNT(*) FROM art_tag_assignments").fetchone()[0])
        oracle_tags = int(conn.execute("SELECT COUNT(*) FROM oracle_tag_assignments").fetchone()[0])
    finally:
        conn.close()

    result = {
        "success": image_stats["failed"] == 0,
        "synced_at": utc_now(),
        "sources": current_sources,
        "printings": printings,
        "tags": tag_stats,
        "images": image_stats,
        "catalog": {
            "printings": total_rows,
            "hashed_printings": total_hashes,
            "missing_hashes": max(0, total_rows - total_hashes),
            "art_tag_assignments": art_tags,
            "oracle_tag_assignments": oracle_tags,
        },
    }
    save_meta(result)
    if image_stats["failed"]:
        print(
            f"[scryfall] incomplete image hash pass: {image_stats['failed']} failures "
            f"will be retried on the next run."
        )
    else:
        print(
            f"[scryfall] catalog ready: {total_rows:,} printings, {total_hashes:,} "
            f"pHashes, {art_tags:,} art tags, {oracle_tags:,} Oracle tags."
        )
    return result


def run_daemon() -> None:
    while True:
        try:
            sync_once()
        except Exception as exc:
            print(f"[scryfall] sync failed: {exc}")
        time.sleep(CHECK_INTERVAL)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Run one update and exit.")
    parser.add_argument("--force", action="store_true", help="Redownload the current bulk snapshots.")
    parser.add_argument("--skip-images", action="store_true", help="Refresh printing metadata and tags only.")
    parser.add_argument("--limit-images", type=int, help="Hash at most this many missing images (useful for a test run).")
    args = parser.parse_args()
    if args.limit_images is not None and args.limit_images < 0:
        parser.error("--limit-images must be zero or greater.")
    if args.once or args.force or args.skip_images or args.limit_images is not None:
        try:
            result = sync_once(
                force=args.force,
                skip_images=args.skip_images,
                image_limit=args.limit_images,
            )
            return 0 if result["success"] else 2
        except Exception as exc:
            print(f"[scryfall] sync failed: {exc}")
            return 1
    run_daemon()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
