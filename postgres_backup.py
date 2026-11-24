"""PostgreSQL custom-format backups with isolated restore verification."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", "/backups"))
DEFAULT_RETENTION = max(1, int(os.environ.get("BACKUP_RETENTION", "2")))
DEFAULT_TIMEZONE = os.environ.get("BACKUP_TIMEZONE", "America/New_York").strip() or "UTC"
REQUIRED_SCHEMAS = ("app", "catalog")
REQUIRED_TABLES = (
    ("app", "people"), ("app", "users"), ("app", "inventory"), ("app", "decklists"),
    ("catalog", "cards"), ("catalog", "printings"), ("catalog", "sets"), ("catalog", "tags"),
)


def _zone(name: str | None = None) -> ZoneInfo:
    value = (name or DEFAULT_TIMEZONE).strip() or "UTC"
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError:
        print(f"[BACKUP] Unknown timezone {value!r}; using UTC", flush=True)
        return ZoneInfo("UTC")


def _pgpass_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:")


@contextmanager
def _credentials(dsn: str | None = None):
    """Yield password-free URI, private PGPASSFILE environment, db and password."""
    raw = (dsn or os.environ.get("CARD_SCANNER_POSTGRES_DSN", "")).strip()
    if not raw:
        raise RuntimeError("CARD_SCANNER_POSTGRES_DSN is not configured")
    parsed = urlsplit(raw)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname or not parsed.username or not parsed.path.strip("/"):
        raise RuntimeError("CARD_SCANNER_POSTGRES_DSN must be a PostgreSQL URI with host, user and database")
    try:
        port = parsed.port or 5432
    except ValueError as exc:
        raise RuntimeError("PostgreSQL DSN has an invalid port") from exc

    query = parse_qsl(parsed.query, keep_blank_values=True)
    passwords = [value for key, value in query if key.lower() == "password"]
    if len(passwords) > 1:
        raise RuntimeError("PostgreSQL DSN contains multiple password parameters")
    if any(key.lower() == "sslpassword" for key, _ in query):
        raise RuntimeError("Do not embed sslpassword in the PostgreSQL DSN")
    password = unquote(parsed.password or "") or (passwords[0] if passwords else "") or os.environ.get("PGPASSWORD", "")
    if "\n" in password or "\r" in password:
        raise RuntimeError("PostgreSQL password cannot contain a newline")
    host = parsed.hostname
    user = unquote(parsed.username)
    database = unquote(parsed.path.lstrip("/"))
    host_uri = f"[{host}]" if ":" in host and not host.startswith("[") else host
    safe_uri = urlunsplit((
        parsed.scheme,
        f"{quote(user, safe='')}@{host_uri}:{port}",
        parsed.path,
        urlencode([(k, v) for k, v in query if k.lower() != "password"]),
        "",
    ))
    env = os.environ.copy()
    env.pop("PGPASSWORD", None)
    env.setdefault("PGCONNECT_TIMEOUT", "15")
    with tempfile.TemporaryDirectory(prefix="cardscanner-pg-auth-") as tmp:
        if password:
            path = Path(tmp) / "pgpass"
            # A wildcard database is required because restore verification also
            # connects to postgres and a uniquely named temporary database.
            row = ":".join(_pgpass_escape(v) for v in (host, str(port), "*", user, password))
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(row + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.chmod(path, 0o600)
            env["PGPASSFILE"] = str(path)
        yield safe_uri, env, database, user, password


def _run(args: list[str], env: dict[str, str], password: str = "") -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if result.returncode:
        error = (result.stderr or "no diagnostic output").replace(password, "[redacted]") if password else (result.stderr or "no diagnostic output")
        raise RuntimeError(f"{Path(args[0]).name} failed ({result.returncode}): {error.strip()[-3000:]}")
    return result


def _identity(dsn: str, env: dict[str, str], password: str = "") -> dict[str, str]:
    query = "SELECT current_database(), current_user, current_setting('server_version');"
    out = _run(["psql", "-X", "--no-password", "-A", "-t", "-F", "|", "--dbname", dsn, "--command", query], env, password).stdout.strip()
    parts = out.split("|", 2)
    if len(parts) != 3 or not all(parts):
        raise RuntimeError("Could not determine PostgreSQL database identity")
    return {"database": parts[0], "user": parts[1], "server_version": parts[2]}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _database_dsn(base: str, database: str) -> str:
    parsed = urlsplit(base)
    return urlunsplit((parsed.scheme, parsed.netloc, "/" + quote(database, safe=""), parsed.query, ""))


def _prune(directory: Path, retention: int) -> None:
    archives = sorted(directory.glob("cardscanner_postgres_*.dump"), key=lambda p: p.stat().st_mtime_ns, reverse=True)
    for old in archives[max(1, retention):]:
        old.unlink(missing_ok=True)
        old.with_suffix(old.suffix + ".json").unlink(missing_ok=True)
        print(f"[BACKUP] Removed expired PostgreSQL backup {old.name}", flush=True)


def create_backup(backup_dir: str | Path | None = None, retention: int | None = None, timezone_name: str | None = None) -> Path:
    directory = Path(backup_dir or DEFAULT_BACKUP_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    keep = max(1, int(DEFAULT_RETENTION if retention is None else retention))
    local_time = datetime.now(_zone(timezone_name))
    created_at = datetime.now(timezone.utc)
    stamp = local_time.strftime("%Y-%m-%d_%H-%M-%S_%f_%z")
    name = f"cardscanner_postgres_{stamp}.dump"
    archive = directory / name
    sidecar = archive.with_suffix(archive.suffix + ".json")

    with _credentials() as (dsn, env, database, _user, password):
        identity = _identity(dsn, env, password)
        if identity["database"] != database:
            raise RuntimeError("Connected database does not match configured DSN")
        expected_database = os.environ.get("CARD_SCANNER_POSTGRES_EXPECTED_DATABASE", "").strip()
        if expected_database and identity["database"] != expected_database:
            raise RuntimeError(
                "PostgreSQL backup safety check failed: "
                f"expected database {expected_database!r}, connected to {identity['database']!r}"
            )
        pg_dump_version = _run(["pg_dump", "--version"], env, password).stdout.strip()
        with tempfile.TemporaryDirectory(prefix=".cardscanner-backup-", dir=directory) as tmp:
            staged = Path(tmp) / name
            metadata_staged = Path(tmp) / (name + ".json")
            _run(["pg_dump", "--no-password", "--format=custom", "--file", str(staged), "--dbname", dsn], env, password)
            if not staged.is_file() or staged.stat().st_size < 1:
                raise RuntimeError("pg_dump returned without a non-empty archive")
            with staged.open("rb") as f:
                os.fsync(f.fileno())
            size = staged.stat().st_size
            digest = _sha256(staged)
            metadata = {
                "schema_version": 1,
                "created_at": created_at.isoformat(),
                "created_at_local": local_time.isoformat(),
                "timezone": getattr(local_time.tzinfo, "key", str(local_time.tzinfo)),
                "database_identity": identity,
                "archive_format": "PostgreSQL custom format",
                "archive_file": name,
                "archive_size_bytes": size,
                "sha256": digest,
                "pg_dump_version": pg_dump_version,
            }
            with metadata_staged.open("x", encoding="utf-8") as f:
                json.dump(metadata, f, indent=2, sort_keys=True)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.chmod(staged, 0o600)
            os.chmod(metadata_staged, 0o600)
            os.replace(staged, archive)
            try:
                os.replace(metadata_staged, sidecar)
            except BaseException:
                archive.unlink(missing_ok=True)
                raise
    _prune(directory, keep)
    print(f"[BACKUP] Created PostgreSQL backup {archive.name} ({size:,} bytes, SHA-256 {digest})", flush=True)
    return archive


def latest_backup(backup_dir: str | Path | None = None) -> Path:
    directory = Path(backup_dir or DEFAULT_BACKUP_DIR)
    files = sorted(directory.glob("cardscanner_postgres_*.dump"), key=lambda p: p.stat().st_mtime_ns, reverse=True)
    if not files:
        raise FileNotFoundError(f"No PostgreSQL backups found in {directory}")
    return files[0]


def _verify_archive_file(archive: Path) -> dict:
    metadata_file = archive.with_suffix(archive.suffix + ".json")
    if not archive.is_file() or not metadata_file.is_file():
        raise FileNotFoundError("Backup archive and its .dump.json sidecar are both required")
    metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
    if archive.stat().st_size != int(metadata.get("archive_size_bytes", -1)):
        raise RuntimeError("Backup archive size does not match its metadata")
    checksum = _sha256(archive)
    if checksum != str(metadata.get("sha256", "")):
        raise RuntimeError("Backup archive SHA-256 does not match its metadata")
    return metadata


def _check_objects(dsn: str, env: dict[str, str], password: str = "") -> None:
    schemas = ",".join(f"('{s}')" for s in REQUIRED_SCHEMAS)
    tables = ",".join(f"('{s}','{t}')" for s, t in REQUIRED_TABLES)
    query = f"""
        WITH s(schema_name) AS (VALUES {schemas}),
             t(schema_name, table_name) AS (VALUES {tables})
        SELECT
          COALESCE((SELECT string_agg(x.schema_name, ', ') FROM s x
            WHERE NOT EXISTS (SELECT 1 FROM information_schema.schemata y WHERE y.schema_name=x.schema_name)), ''),
          COALESCE((SELECT string_agg(x.schema_name||'.'||x.table_name, ', ') FROM t x
            WHERE to_regclass(format('%I.%I',x.schema_name,x.table_name)) IS NULL), '');
    """
    out = _run(["psql", "-X", "--no-password", "-A", "-t", "-F", "|", "--dbname", dsn, "--command", query], env, password).stdout.strip()
    parts = out.split("|", 1)
    if len(parts) != 2:
        raise RuntimeError("Restore verification query returned an invalid result")
    missing = []
    if parts[0]:
        missing.append("schemas: " + parts[0])
    if parts[1]:
        missing.append("tables: " + parts[1])
    if missing:
        raise RuntimeError("Restored database is missing required " + "; ".join(missing))


def verify_backup(archive_path: str | Path | None = None, backup_dir: str | Path | None = None) -> dict:
    archive = Path(archive_path).expanduser().resolve() if archive_path else latest_backup(backup_dir).resolve()
    metadata = _verify_archive_file(archive)
    with _credentials() as (dsn, env, database, _user, password):
        identity = _identity(dsn, env, password)
        expected_database = os.environ.get("CARD_SCANNER_POSTGRES_EXPECTED_DATABASE", "").strip()
        if expected_database and identity["database"] != expected_database:
            raise RuntimeError(
                "PostgreSQL backup verification safety check failed: "
                f"expected database {expected_database!r}, connected to {identity['database']!r}"
            )
        backup_identity = metadata.get("database_identity") or {}
        if identity["database"] != database or backup_identity.get("database") != database:
            raise RuntimeError("Backup database identity does not match the configured database")
        maintenance = _database_dsn(dsn, "postgres")
        scratch = "cardscanner_verify_" + uuid4().hex[:12]
        created = False
        removed = False
        try:
            _run(["createdb", "--no-password", f"--maintenance-db={maintenance}", "--template=template0", "--encoding=UTF8", scratch], env, password)
            created = True
            target = _database_dsn(dsn, scratch)
            _run(["pg_restore", "--no-password", "--exit-on-error", "--single-transaction", "--no-owner", "--no-privileges", "--dbname", target, str(archive)], env, password)
            _check_objects(target, env, password)
        finally:
            if created:
                try:
                    _run(["dropdb", "--no-password", "--if-exists", "--force", f"--maintenance-db={maintenance}", scratch], env, password)
                    removed = True
                except Exception as exc:
                    raise RuntimeError(f"Could not remove temporary verification database {scratch}: {exc}") from exc
    return {
        "verified": True,
        "archive": archive.name,
        "database": database,
        "archive_size_bytes": archive.stat().st_size,
        "sha256": str(metadata["sha256"]),
        "required_schemas": list(REQUIRED_SCHEMAS),
        "required_tables": [f"{s}.{t}" for s, t in REQUIRED_TABLES],
        "temporary_database_removed": removed,
    }


def restore_backup(archive_path: str | Path | None, target_database: str, *, backup_dir: str | Path | None = None, create_database: bool = False) -> dict:
    archive = Path(archive_path).expanduser().resolve() if archive_path else latest_backup(backup_dir).resolve()
    metadata = _verify_archive_file(archive)
    target_name = str(target_database).strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$-]{0,62}", target_name):
        raise ValueError("target database must be a valid PostgreSQL identifier (1-63 characters)")
    with _credentials() as (dsn, env, current_database, _user, password):
        expected_database = os.environ.get("CARD_SCANNER_POSTGRES_EXPECTED_DATABASE", "").strip()
        if expected_database and current_database != expected_database:
            raise RuntimeError(
                "PostgreSQL restore safety check failed: "
                f"expected configured database {expected_database!r}, connected to {current_database!r}"
            )
        if target_name == current_database:
            raise RuntimeError("Refusing to restore over the database configured in CARD_SCANNER_POSTGRES_DSN")
        backup_identity = metadata.get("database_identity") or {}
        if backup_identity.get("database") != current_database:
            raise RuntimeError("Backup belongs to a different configured source database")
        maintenance = _database_dsn(dsn, "postgres")
        target = _database_dsn(dsn, target_name)
        created = False
        completed = False
        try:
            if create_database:
                _run(["createdb", "--no-password", f"--maintenance-db={maintenance}", "--template=template0", "--encoding=UTF8", target_name], env, password)
                created = True
            _run(["pg_restore", "--no-password", "--exit-on-error", "--single-transaction", "--no-owner", "--no-privileges", "--dbname", target, str(archive)], env, password)
            _check_objects(target, env, password)
            completed = True
        finally:
            if created and not completed:
                _run(["dropdb", "--no-password", "--if-exists", "--force", f"--maintenance-db={maintenance}", target_name], env, password)
    return {
        "restored": True,
        "archive": archive.name,
        "target_database": target_name,
        "archive_size_bytes": archive.stat().st_size,
        "sha256": str(metadata["sha256"]),
        "required_schemas": list(REQUIRED_SCHEMAS),
        "required_tables": [f"{s}.{t}" for s, t in REQUIRED_TABLES],
    }


def verify_main() -> int:
    parser = argparse.ArgumentParser(description="Verify a PostgreSQL backup by restoring it into a temporary database.")
    parser.add_argument("archive", nargs="?")
    parser.add_argument("--backup-dir", default=str(DEFAULT_BACKUP_DIR))
    args = parser.parse_args()
    print(json.dumps(verify_backup(args.archive, args.backup_dir), indent=2, sort_keys=True))
    return 0


def restore_main() -> int:
    parser = argparse.ArgumentParser(description="Restore a PostgreSQL backup into a separate database.")
    parser.add_argument("--backup")
    parser.add_argument("--backup-dir", default=str(DEFAULT_BACKUP_DIR))
    parser.add_argument("--target-database", required=True)
    parser.add_argument("--create-database", action="store_true")
    args = parser.parse_args()
    print(json.dumps(restore_backup(args.backup, args.target_database, backup_dir=args.backup_dir, create_database=args.create_database), indent=2, sort_keys=True))
    return 0
