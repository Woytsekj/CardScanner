"""PostgreSQL-native Scryfall bulk synchronization and operational status."""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import scryfall_updater as http
from postgres_app_db import connect_app_database
from postgres_db import close_pool, open_pool, run_migrations
from scryfall_postgres import fill_missing_hashes, import_printings, import_tag_snapshots

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
CHECK_INTERVAL = max(3600, int(os.environ.get("SCRYFALL_CATALOG_CHECK_INTERVAL", "86400")))
RETRY_MIN = max(30, int(os.environ.get("SCRYFALL_RETRY_MIN_SECONDS", "60")))
RETRY_MAX = max(RETRY_MIN, int(os.environ.get("SCRYFALL_RETRY_MAX_SECONDS", "900")))
GAME = "mtg"
PROVIDER = "scryfall"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect():
    open_pool()
    # Validate the configured database identity before applying migrations.
    guard = connect_app_database()
    guard.close()
    run_migrations()
    return connect_app_database()


def fetch_sync_status() -> dict[str, Any]:
    """Fetch persisted sync state for the administrator Server Info page."""
    conn = _connect()
    try:
        row = conn.execute(
            """SELECT last_attempt_at, last_success_at, source_revision,
                      import_counts, last_error, updated_at
               FROM catalog.provider_sync_state
               WHERE game_id=%s AND provider_id=%s""",
            (GAME, PROVIDER),
        ).fetchone()
        if not row:
            return {
                "configured": True, "last_attempt_at": None, "last_success_at": None,
                "source_revision": {}, "import_counts": {}, "last_error": None,
            }
        return {"configured": True, **dict(row)}
    finally:
        conn.close()


def _write_status(conn, *, attempted_at: str, successful_at: str | None = None,
                  source_revision: dict[str, str] | None = None,
                  import_counts: dict[str, Any] | None = None,
                  error: str | None = None) -> None:
    conn.execute(
        """INSERT INTO catalog.provider_sync_state (
               game_id, provider_id, last_attempt_at, last_success_at,
               source_revision, import_counts, last_error, updated_at
           )
           VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, now())
           ON CONFLICT (game_id, provider_id) DO UPDATE SET
               last_attempt_at=EXCLUDED.last_attempt_at,
               last_success_at=COALESCE(EXCLUDED.last_success_at, catalog.provider_sync_state.last_success_at),
               source_revision=CASE WHEN EXCLUDED.last_success_at IS NULL
                   THEN catalog.provider_sync_state.source_revision ELSE EXCLUDED.source_revision END,
               import_counts=CASE WHEN EXCLUDED.last_success_at IS NULL
                   THEN catalog.provider_sync_state.import_counts ELSE EXCLUDED.import_counts END,
               last_error=EXCLUDED.last_error,
               updated_at=now()""",
        (
            GAME, PROVIDER, attempted_at, successful_at,
            json.dumps(source_revision or {}, separators=(",", ":")),
            json.dumps(import_counts or {}, separators=(",", ":")),
            error,
        ),
    )


def _catalog_counts(conn) -> dict[str, int]:
    queries = {
        "printings": ("SELECT COUNT(*) AS count FROM catalog.printings WHERE game_id=%s", (GAME,)),
        "hashed_images": ("""SELECT COUNT(DISTINCT image_id) AS count FROM catalog.image_hashes
                             WHERE game_id=%s AND hash_algorithm='phash' AND hash_version='1'""", (GAME,)),
        "tags": ("SELECT COUNT(*) AS count FROM catalog.tags WHERE game_id=%s AND provider_id=%s", (GAME, PROVIDER)),
        "art_assignments": ("SELECT COUNT(*) AS count FROM catalog.art_tag_assignments WHERE game_id=%s AND provider_id=%s", (GAME, PROVIDER)),
        "oracle_assignments": ("SELECT COUNT(*) AS count FROM catalog.oracle_tag_assignments WHERE game_id=%s AND provider_id=%s", (GAME, PROVIDER)),
    }
    return {
        name: int(conn.execute(sql, params).fetchone()["count"])
        for name, (sql, params) in queries.items()
    }


def sync_once(*, force: bool = False, skip_images: bool = False,
              image_limit: int | None = None) -> dict[str, Any]:
    """Refresh changed Scryfall snapshots; interrupted transactions leave prior rows intact."""
    if image_limit is not None and int(image_limit) < 0:
        raise ValueError("image_limit must be zero or greater")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    manifest = http.bulk_manifest()
    source_revision = {
        name: str(item.get("updated_at") or "")
        for name, item in manifest.items()
    }
    required = ("default_cards", "art_tags", "oracle_tags")
    if any(not source_revision.get(name) for name in required):
        raise RuntimeError("Scryfall manifest is missing a source revision for a required bulk export")

    attempted_at = utc_now()
    conn = _connect()
    try:
        with conn:
            _write_status(conn, attempted_at=attempted_at)

        previous = conn.execute(
            "SELECT source_revision FROM catalog.provider_sync_state WHERE game_id=%s AND provider_id=%s",
            (GAME, PROVIDER),
        ).fetchone()
        previous_sources = dict(previous["source_revision"] or {}) if previous else {}
        counts_before = _catalog_counts(conn)

        refresh_printings = (
            force or counts_before["printings"] == 0
            or previous_sources.get("default_cards") != source_revision["default_cards"]
        )
        refresh_tags = (
            force or counts_before["tags"] == 0
            or previous_sources.get("art_tags") != source_revision["art_tags"]
            or previous_sources.get("oracle_tags") != source_revision["oracle_tags"]
        )
        counts: dict[str, Any] = {
            "printings": {"printings": 0, "with_images": 0},
            "tags": {"tags": 0, "aliases": 0, "art_assignments": 0, "oracle_assignments": 0},
            "images": {"attempted": 0, "updated": 0, "failed": 0},
            "changed_sources": [],
        }

        if refresh_printings:
            print(f"[scryfall-postgres] downloading default_cards ({source_revision['default_cards']})", flush=True)
            path = http.download_bulk(manifest["default_cards"], "default_cards")
            counts["printings"] = import_printings(conn, path, source_revision["default_cards"])
            counts["changed_sources"].append("default_cards")
        else:
            print("[scryfall-postgres] default_cards unchanged; retaining PostgreSQL printings", flush=True)

        if refresh_tags:
            print("[scryfall-postgres] downloading art_tags and oracle_tags", flush=True)
            art_path = http.download_bulk(manifest["art_tags"], "art_tags")
            oracle_path = http.download_bulk(manifest["oracle_tags"], "oracle_tags")
            counts["tags"] = import_tag_snapshots(
                conn, art_path, oracle_path,
                {"art_tags": source_revision["art_tags"], "oracle_tags": source_revision["oracle_tags"]},
            )
            counts["changed_sources"].extend(("art_tags", "oracle_tags"))
        else:
            print("[scryfall-postgres] Tagger snapshots unchanged; retaining PostgreSQL assignments", flush=True)

        if not skip_images:
            # The importer writes only after checking that the image URI still
            # matches; failed hashes remain missing and are retried next pass.
            counts["images"] = fill_missing_hashes(
                conn,
                limit=image_limit,
                workers=max(1, min(8, int(os.environ.get("SCRYFALL_IMAGE_WORKERS", "4")))),
                batch_size=128,
            )

        catalog = _catalog_counts(conn)
        counts["catalog"] = catalog
        successful_at = utc_now()
        with conn:
            _write_status(
                conn, attempted_at=attempted_at, successful_at=successful_at,
                source_revision=source_revision,
                import_counts=counts,
                error=(
                    f"{counts['images']['failed']} image hashes failed and will be retried"
                    if counts["images"]["failed"] else None
                ),
            )

        result = {
            "success": counts["images"]["failed"] == 0,
            "last_attempt_at": attempted_at,
            "last_success_at": successful_at,
            "source_revision": source_revision,
            "import_counts": counts,
            "catalog": catalog,
        }
        if counts["images"]["failed"]:
            print(f"[scryfall-postgres] {counts['images']['failed']} hashes remain pending for retry", flush=True)
        else:
            print(
                f"[scryfall-postgres] sync complete: {catalog['printings']:,} printings, "
                f"{catalog['hashed_images']:,} hashed images and {catalog['tags']:,} tags",
                flush=True,
            )
        return result
    except Exception as exc:
        try:
            conn.rollback()
            with conn:
                _write_status(
                    conn, attempted_at=attempted_at,
                    error=f"{type(exc).__name__}: {exc}"[:4000],
                )
        except Exception as status_error:
            print(f"[scryfall-postgres] could not persist failure status: {status_error}", flush=True)
        raise
    finally:
        conn.close()


def run_daemon() -> None:
    retry = RETRY_MIN
    while True:
        try:
            result = sync_once()
            if result["success"]:
                retry = RETRY_MIN
                time.sleep(CHECK_INTERVAL)
            else:
                retry = min(RETRY_MAX, max(RETRY_MIN, retry * 2))
                print(f"[scryfall-postgres] retrying incomplete image hashing in {retry}s", flush=True)
                time.sleep(retry)
        except Exception as exc:
            print(f"[scryfall-postgres] sync failed; retrying: {exc}", flush=True)
            time.sleep(retry)
            retry = min(RETRY_MAX, max(RETRY_MIN, retry * 2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--skip-images", action="store_true")
    parser.add_argument("--limit-images", type=int)
    args = parser.parse_args(argv)
    if args.limit_images is not None and args.limit_images < 0:
        parser.error("--limit-images must be zero or greater")
    try:
        if args.once or args.force or args.skip_images or args.limit_images is not None:
            result = sync_once(force=args.force, skip_images=args.skip_images, image_limit=args.limit_images)
            return 0 if result["success"] else 2
        run_daemon()
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"[scryfall-postgres] sync failed: {exc}", flush=True)
        return 1
    finally:
        close_pool()


if __name__ == "__main__":
    raise SystemExit(main())
