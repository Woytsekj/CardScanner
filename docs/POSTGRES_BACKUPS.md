# PostgreSQL backups and recovery

CardScanner's PostgreSQL backup worker writes PostgreSQL custom-format archives (`.dump`) to a separately mounted backup destination. It does not copy or modify the live PostgreSQL data directory.

## Configure the backup destination

Set these values in the deployment `.env` used by Docker Compose:

```dotenv
CARD_SCANNER_DATABASE_BACKEND=postgres
CARD_SCANNER_POSTGRES_DSN=postgresql://cardscanner:URL_ENCODED_PASSWORD@postgres-host:5432/cardscanner
CARD_SCANNER_POSTGRES_EXPECTED_DATABASE=cardscanner
BACKUP_HOST_DIR=/mnt/nas/cardscanner-postgres-backups
BACKUP_HOUR=2
BACKUP_MINUTE=0
BACKUP_TIMEZONE=America/New_York
BACKUP_RETENTION=2
BACKUP_CHECK_INTERVAL=30
```

Use a PostgreSQL URI and percent-encode reserved characters in the password. The password is removed from the URI passed to PostgreSQL client processes and supplied through a private temporary `PGPASSFILE` (mode `0600`). It is not written to application logs or backup metadata. Do not put credentials in command-line arguments or commit the deployment `.env`.

`BACKUP_HOST_DIR` is mounted into the dedicated backup container at `/backups`. Point it at storage separate from PostgreSQL's live data volume; a NAS backup share is appropriate. The default is `./data/backups` to preserve the existing SQLite backup destination. The default schedule is 02:00 America/New_York, retaining the newest two successful dumps.

The backup container uses PostgreSQL 17 client utilities. Keep the client major version at least as new as the PostgreSQL server major version; update `Dockerfile.backup` when upgrading the server.

## What a successful backup contains

The worker runs `pg_dump --format=custom` and writes an archive plus a same-name `.dump.json` sidecar. The sidecar records the UTC and local creation timestamps, configured time zone, database name, connected role, server version, archive size, SHA-256 checksum, and `pg_dump` version. Files are staged before publication and have mode `0600`. Retention pruning occurs only after a new archive and sidecar have both been written successfully.

Check the service:

```bash
docker compose logs --tail=100 backup
```

Run a backup immediately:

```bash
docker compose exec backup python3 backup_database.py --once
```

## Verify a backup by restoring it

Run the verification command against the newest archive:

```bash
docker compose exec backup python3 verify_postgres_backup.py
```

Or specify a particular archive:

```bash
docker compose exec backup python3 verify_postgres_backup.py /backups/cardscanner_postgres_YYYY-MM-DD_HH-MM-SS_+0000.dump
```

Verification checks the sidecar's byte size and SHA-256 checksum, creates a randomly named temporary database from `template0`, restores the archive in a single transaction, and verifies the `app` and `catalog` schemas plus core user, inventory, deck, printing, set, and tag tables. The temporary database is dropped after either success or failure.

The configured PostgreSQL role needs `CREATEDB` permission for restore verification. It also needs enough free storage for a second copy of the database during the test. Schedule verification for a quieter period on large production databases; it uses real restore I/O and disk capacity, not just a fast archive-header check.

## Recovery into a replacement database

The recovery tool only restores to a *different* database. It refuses to target the database currently named in `CARD_SCANNER_POSTGRES_DSN`, and never drops an existing target database.

1. Stop the CardScanner web and worker services so there are no application writes during cutover.
2. Verify the desired archive:

   ```bash
   docker compose exec backup python3 verify_postgres_backup.py
   ```

3. Restore the newest archive into a new database. The command creates the target when requested, restores it, verifies the expected schemas/tables, and removes a newly created target if the restore fails:

   ```bash
   docker compose exec backup python3 restore_postgres_backup.py --target-database cardscanner_recovered --create-database
   ```

   To choose an archive explicitly, add `--backup /backups/archive-name.dump`.

4. Review the restore result, then change `CARD_SCANNER_POSTGRES_DSN` to point to `cardscanner_recovered`. Make sure `CARD_SCANNER_POSTGRES_EXPECTED_DATABASE` matches the new database name.
5. Start the app and workers and perform login, inventory, deck, pricing, Scryfall catalog, and scanner smoke tests.
6. Keep the original database and backup archive untouched until the replacement has been checked. Do not remove the old database as part of the restore command.

Restores use `--no-owner --no-privileges`: restored objects are owned by the role used in the configured DSN, without requiring every original role/ACL on the recovery cluster. The source archive still contains the original ownership and privilege records.

For disaster recovery, keep an independent/off-site copy of the NAS backup directory. A backup stored beside the live database is not protection against loss of the host or storage appliance.
