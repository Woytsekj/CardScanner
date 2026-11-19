# PostgreSQL development environment

This is an isolated development/test PostgreSQL service. It is not part of the production Compose deployment and does not connect to the production database.

## Prerequisites

- Docker Engine and Docker Compose plugin
- Python 3.12 for running the integration test from the host (or use the existing project image after dependencies are installed)

## Start the disposable development database

From the repository root:

```bash
cp .env.postgres.example .env.postgres
```

Edit `.env.postgres` and replace the sample password with a long random development-only password. The file is ignored by Git.

Start only the isolated PostgreSQL service:

```bash
docker compose --env-file .env.postgres -f docker-compose.postgres-dev.yml --profile postgres-dev up -d
docker compose --env-file .env.postgres -f docker-compose.postgres-dev.yml --profile postgres-dev ps
```

The service binds to 127.0.0.1:5433 and stores its data in the named Docker volume `cardscanner_pg_dev_data`. It does not mount the production data directory.

## Install Python dependencies and run the integration test

Use a virtual environment, then:

```bash
python -m pip install -r requirements.txt
export CARD_SCANNER_POSTGRES_DSN="$(sed -n 's/^CARD_SCANNER_POSTGRES_DSN=//p' .env.postgres)"
python -m unittest tests.test_postgres_foundation tests.test_postgres_tcgcsv -v
```

The integration suites apply the ordered versioned migrations to the
database selected by `CARD_SCANNER_POSTGRES_DSN`. The foundation tests cover
game/provider registration, catalog and application constraints, metadata,
price history, and migration idempotency. The TCGCSV tests cover provider
groups, products, category scope, exact subtype observations, and source
timestamp preservation. Test fixtures are rolled back.

At the 2026-11-14 development checkpoint, all 13 tests passed with migrations
0001–0007 applied. This validates the development schema and its tested
constraints; it does not validate a production data importer or application
cutover.

The expected initial TCGCSV category IDs are verified against the live categories endpoint at the time of this design:
- MTG: 1
- Pokémon: 3
- One Piece Card Game: 68
- Riftbound: 89

The updater must still validate category IDs against TCGCSV's categories feed at runtime; this seed is configuration, not permission to blindly sync a category if the provider changes it.

## Stop the development database

```bash
docker compose --env-file .env.postgres -f docker-compose.postgres-dev.yml --profile postgres-dev down
```

This leaves the named development volume intact. To intentionally delete this disposable development database later, remove the named volume explicitly after confirming it contains no needed test data. Never use this volume for production.

## Safety boundary

- Do not add this PostgreSQL service to the live production Compose project yet.
- Do not set `CARD_SCANNER_POSTGRES_DSN` in production.
- Do not migrate, rename, delete, or overwrite either production SQLite database.
- Do not change the existing production deployment configuration, environment files, staged changes, stash, or backup files as part of this phase.
