from __future__ import annotations

import argparse
import os
import sqlite3
import tarfile
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", DATA_DIR / "backups"))
BACKUP_HOUR = max(0, min(23, int(os.environ.get("BACKUP_HOUR", "2"))))
BACKUP_MINUTE = max(0, min(59, int(os.environ.get("BACKUP_MINUTE", "0"))))
BACKUP_TIMEZONE = os.environ.get("BACKUP_TIMEZONE", "America/New_York").strip() or "UTC"
BACKUP_RETENTION = max(1, int(os.environ.get("BACKUP_RETENTION", "2")))
CHECK_INTERVAL_SECONDS = max(15, int(os.environ.get("BACKUP_CHECK_INTERVAL", "30")))

DATABASES = (
    "mtg_inventory.db",
    "mtg_art_hashes.db",
)


def get_timezone() -> ZoneInfo:
    try:
        return ZoneInfo(BACKUP_TIMEZONE)
    except ZoneInfoNotFoundError:
        print(f"[BACKUP] Unknown timezone {BACKUP_TIMEZONE!r}; falling back to UTC", flush=True)
        return ZoneInfo("UTC")


def backup_sqlite(source: Path, destination: Path) -> None:
    """Create a consistent SQLite snapshot, including WAL changes."""
    source_conn = sqlite3.connect(source, timeout=60)
    try:
        source_conn.execute("PRAGMA busy_timeout=5000")
        dest_conn = sqlite3.connect(destination)
        try:
            source_conn.backup(dest_conn, pages=100, sleep=0.10)
            dest_conn.commit()
        finally:
            dest_conn.close()
    finally:
        source_conn.close()


def create_backup() -> Path | None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    existing = [DATA_DIR / name for name in DATABASES if (DATA_DIR / name).exists()]
    if not existing:
        print(f"[BACKUP] No SQLite databases found in {DATA_DIR}; skipping", flush=True)
        return None

    now = datetime.now(get_timezone())
    stamp = now.strftime("%Y-%m-%d_%H-%M-%S_%Z")
    archive_path = BACKUP_DIR / f"mtg_database_{stamp}.tar.gz"

    with tempfile.TemporaryDirectory(prefix="mtg-backup-", dir=str(BACKUP_DIR)) as temp_dir:
        temp_path = Path(temp_dir)
        copied = []
        for source in existing:
            destination = temp_path / source.name
            print(f"[BACKUP] Snapshotting {source.name}", flush=True)
            backup_sqlite(source, destination)
            copied.append(destination)

        manifest = temp_path / "backup_info.txt"
        manifest.write_text(
            "MTG Scanner database backup\n"
            f"Created: {now.isoformat()}\n"
            f"Timezone: {BACKUP_TIMEZONE}\n"
            f"Databases: {', '.join(path.name for path in copied)}\n",
            encoding="utf-8",
        )

        with tarfile.open(archive_path, "w:gz") as archive:
            for path in copied:
                archive.add(path, arcname=path.name)
            archive.add(manifest, arcname=manifest.name)

    os.chmod(archive_path, 0o600)
    print(f"[BACKUP] Created {archive_path}", flush=True)

    backups = sorted(BACKUP_DIR.glob("mtg_database_*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old_backup in backups[BACKUP_RETENTION:]:
        try:
            old_backup.unlink()
            print(f"[BACKUP] Removed old backup {old_backup}", flush=True)
        except FileNotFoundError:
            pass

    return archive_path


def run_scheduler(run_once: bool) -> None:
    tz = get_timezone()
    if run_once:
        create_backup()
        return

    last_run_date = None
    print(
        f"[BACKUP] Nightly database backup scheduled for {BACKUP_HOUR:02d}:{BACKUP_MINUTE:02d} "
        f"{BACKUP_TIMEZONE}; retaining last {BACKUP_RETENTION}",
        flush=True,
    )

    while True:
        now = datetime.now(tz)
        if now.hour == BACKUP_HOUR and now.minute == BACKUP_MINUTE and now.date() != last_run_date:
            try:
                create_backup()
                last_run_date = now.date()
            except Exception as exc:
                print(f"[BACKUP] Backup failed: {exc}", flush=True)
                last_run_date = now.date()

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Nightly SQLite database backup for MTG Scanner")
    parser.add_argument("--once", action="store_true", help="Run one backup immediately and exit")
    args = parser.parse_args()
    run_scheduler(run_once=args.once)
