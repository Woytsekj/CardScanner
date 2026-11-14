# ⚠️ This is vibe coded AF. Use at your own risk.

This project was built with heavy AI-assisted (“vibe-coded”) development. It may contain bugs, security issues, incomplete features, or data-loss risks. **Use it at your own risk. Back up your databases before upgrading or experimenting.** Do not expose it to the public internet without reviewing the code and configuring access controls properly.

# MTG Scanner — Inventory Web App

A self-hosted Magic: The Gathering collection and inventory manager with a companion scanner client. The web application is built with Python, Flask, SQLite, and Docker. It tracks card ownership and location, decklists, pricing, and price history.

Repository: <https://github.com/Woytsekj/CardScanner>

## Project layout

- `app.py` — Flask web application and API.
- `templates/`, `static/` — web interface and styles.
- `price_updater.py` — TCGCSV price updates and optional Discord price alerts.
- `backup_database.py` — scheduled SQLite backups.
- `migrate_storage.py` — guarded migration of SQLite databases from the data volume to local database storage.
- `update_scryfall_metadata.py` — refreshes cached card metadata from Scryfall bulk data.
- `scanner-client/` — standalone desktop scanning application, camera helpers, and installable Android-friendly PWA.
- `docker-compose.yml` — web app, price updater, and backup services.
- `.env.example` — configuration template; copy it to `.env` and set your own values.

## Features

- Multi-owner inventory with normal and foil finishes.
- Free-storage and deck-based inventory locations.
- Deck management, decklist comparison, and Archidekt deck import.
- Fuzzy deck matching across printings using Oracle identity where available.
- Cached Oracle text, type line, mana cost, legality, and alternate/printed-name aliases.
- TCGCSV price synchronization and historical price charts.
- Optional Discord price alerts.
- Vending view for browsing and selling cards, including CSV export and printable labels.
- User roles for administration and vending.
- Scheduled database backups with configurable retention.
- A companion desktop scanner that sends scans through the dedicated scanner API.
- An installable Android-friendly scanner PWA at `/scanner/mobile/`, using the phone camera directly and the logged-in user session.
- Scanner-side account/person mapping, separate physical-location and vending fields, and idempotent scan retries.

Features and behavior may change. See `CHANGES_inventory_overhaul.md`, [`CHANGES_scanner_integration_v39.md`](CHANGES_scanner_integration_v39.md), and the code for the current implementation.

## Requirements

- Linux server or another environment that can run Docker Compose.
- Docker Engine and the Docker Compose plugin.
- Persistent storage for the application databases.
- For public access: a properly configured reverse proxy or Cloudflare Tunnel, plus an access-control layer.

## Quick start

1. Clone the repository:

   ```bash
   git clone https://github.com/Woytsekj/CardScanner.git
   cd CardScanner
   ```

2. Create the local environment file:

   ```bash
   cp .env.example .env
   ```

3. Edit `.env`. At minimum, set unique, strong values for:
   - `SESSION_SECRET`
   - `SCANNER_API_KEY`
   - `ADMIN_PASSWORD` (used to bootstrap the initial admin account when the database has no users)
   - `TRUSTED_HOSTS` and `LOCAL_HOSTS` to match your actual access method

   Generate secrets rather than reusing passwords. Use values of at least 32 random characters for `SESSION_SECRET` and `SCANNER_API_KEY`. Never commit `.env`.

4. Prepare the persistent data and local database directories:

   ```bash
   mkdir -p data
   sudo mkdir -p /var/lib/cardscanner/db
   ```

   Set `DB_HOST_DIR=/var/lib/cardscanner/db` in `.env` (this is also the Compose default). Keep the active `mtg_inventory.db` and `mtg_art_hashes.db` files in this local directory. Scryfall bulk snapshots, TCGCSV JSON files, sync metadata, and backup archives remain under `data/`, which may be an NFS mount. If running containers as a non-root user, make the local database directory writable by the configured `APP_UID:APP_GID`.

   For an existing NFS-backed installation, **do not start the updated services yet**. Follow the existing-install migration steps in the Storage layout section below; the migration utility verifies both databases and never deletes or overwrites the originals.

5. Build and start the services:

   ```bash
   docker compose up -d --build
   ```

6. Check service status and logs:

   ```bash
   docker compose ps
   docker compose logs -f web
   ```

   The default host port is `5000`. If configured for local/LAN use, open `http://SERVER-IP:5000` from a permitted device. The initial admin credentials are controlled by `.env` when the database is first initialized.

## Services

Compose defines four services:

- **web** — Flask application served by Gunicorn.
- **updater** — periodically checks TCGCSV data and updates prices/history.
- **backup** — creates SQLite backups on the configured schedule and prunes older archives according to retention settings.
- **scryfall** — checks Scryfall's `default_cards`, `art_tags`, and `oracle_tags` bulk feeds daily, incrementally builds pHashes for new/changed printing images, and refreshes local tag indexes.

Large application data, Scryfall bulk snapshots, price JSON files, and backup archives remain under `./data/` on the host (which can be an NFS share). Active SQLite databases are mounted separately from `DB_HOST_DIR`, defaulting to `/var/lib/cardscanner/db` on the Docker host's local filesystem. Inside every service, this location is available as `/db` through `DB_DIR`. Keep `DB_HOST_DIR` on local storage: SQLite WAL databases must not be placed on network filesystems. The backup service snapshots the local databases and writes the resulting archives to `./data/backups/` on the NAS. The default backup settings keep the newest two archives and schedule backups for 02:00 in `America/New_York`; see `.env.example` to customize them.

Useful commands:

```bash
docker compose ps
docker compose logs -f web
docker compose logs -f updater
docker compose logs -f backup
docker compose restart web
docker compose down
```

`docker compose down` stops and removes containers/networks; it does not remove the bind-mounted `data/` directory. Do not use `docker compose down -v` casually, and always maintain independent backups.

## Storage layout and existing-install migration

CardScanner separates frequently accessed SQLite databases from large data files. `DB_HOST_DIR` is a host path on local storage (default `/var/lib/cardscanner/db`); Compose mounts it as `/db` in the web, updater, Scryfall, and backup services. The NFS-backed `./data/` directory remains the home for Scryfall bulk downloads, price snapshots, sync metadata, and compressed backups.

For an existing installation whose two SQLite files are still in `./data/`, migrate them before starting the updated services:

1. While the old containers are still running, create a fresh backup on the NAS:

   ```bash
   docker compose exec backup python backup_database.py --once
   ```

2. Pull the updated code, create `DB_HOST_DIR` on the local disk, and ensure it is writable by the container's configured `APP_UID:APP_GID`. Build the updated images, but do not start/recreate the services yet:

   ```bash
   sudo mkdir -p /var/lib/cardscanner/db
   docker compose build
   docker compose stop web updater scryfall backup
   ```

3. With all database users stopped, run the guarded copy and verification commands:

   ```bash
   docker compose run --rm --no-deps web python migrate_storage.py --copy
   docker compose run --rm --no-deps web python migrate_storage.py --verify
   ```

   The utility uses SQLite's backup API (including committed WAL contents), validates both source and destination files, refuses to overwrite an existing destination, and leaves the original NFS databases untouched.

4. Start the services and confirm that the collection, decks, card details, and scanner catalog load correctly:

   ```bash
   docker compose up -d
   docker compose ps
   docker compose logs --tail=80 web updater scryfall backup
   ```

   Keep the original NFS database files until the new deployment has been fully checked and a fresh NAS backup has completed. The migration is reversible: stop the services, point `DB_HOST_DIR` back at a verified local copy or restore the original files, and start again. Never run both database locations as active writers.

## Scanner client

The scanner is a separate desktop application in [`scanner-client/`](scanner-client/). It uses a camera feed, image processing, OCR, and the server's dedicated scanner API. It can be set up and run independently of the web container. From the inventory page, use **Mobile Scanner** to open the phone-camera PWA or **Download Desktop Scanner** to download a Linux source bundle with setup instructions and a sample configuration. The desktop download is source, not a compiled installer; configure your own server URL and scanner API key after downloading.

Start with [`scanner-client/README.md`](scanner-client/README.md) for client dependencies, camera setup, configuration, and troubleshooting.

The desktop scanner requires a server URL and scanner API key. Configure these in the client-local `scanner.env.fish` file, based on `scanner.env.fish.example`; never commit or share that secret. The desktop app can synchronize the current production `mtg_art_hashes.db` and `tcg_prices.json` over authenticated scanner API endpoints at startup or with its **Sync Server Data** button. The server exposes only an allow-listed manifest and those two downloadable data files; the client verifies SHA256 and file integrity before atomic replacement. The dedicated `scryfall` service now keeps `mtg_art_hashes.db` current from Scryfall's `default_cards` printing export: it adds new printings, refreshes printing metadata, preserves valid existing hashes, and generates missing or changed-image pHashes using the same ImageHash algorithm as the desktop scanner. It also imports the community `art_tags` and `oracle_tags` Tagger datasets into indexed tables in that same SQLite file, so the existing desktop sync distributes them too.

Inventory search supports `art:tag-name` / `atag:tag-name` for illustration tags and `otag:tag-name` / `function:tag-name` for Oracle/function tags. Matching card tags are shown as chips in both inventory grid and list views. Multi-face printings map all face illustration IDs, so art tags can match either face. The Scryfall status line reports printings, completed pHashes, and art/Oracle tagging counts after a sync.

The first run downloads Scryfall's current bulk snapshots and generates hashes for printings that do not already have a usable pHash; that initial pass is the heaviest run. Image requests are rate-limited and parallelism is configurable. Subsequent runs only re-download changed bulk feeds and hash missing/changed image URLs. Failed image hashes remain pending and are retried by the scheduled service. The Android-friendly PWA is served by the main Flask app at `https://YOUR-CARDS-HOST/scanner/mobile/` and uses a logged-in CardScanner session instead of embedding the scanner key. It is an installable web app, not a Play Store APK; on Android, open it in Chrome and choose Install app / Add to Home screen. It uses OCR plus manual exact-printing confirmation, while the desktop client retains the existing image-matching engine. Follow [`scanner-client/SERVER_INTEGRATION.md`](scanner-client/SERVER_INTEGRATION.md) for deployment and Cloudflare Access guidance. The current user model links one login to at most one default person, and non-admin mobile access is restricted server-side to that linked person.

## Security notes — read before exposing this app

- **Do not port-forward TCP port 5000 directly from your router to the internet.**
- For the existing intended public deployment, put the app behind a Cloudflare Tunnel and Cloudflare Access (or an equivalent properly configured access-control proxy).
- A tunnel alone is not an authentication system. Configure Access policies for the people who should be able to sign in.
- Use strong, unique `SESSION_SECRET`, `SCANNER_API_KEY`, and account passwords.
- The scanner API key is separate from a web administrator login, but it still grants access to the specific scanner endpoints. Treat it as a secret.
- Never commit `.env`, database files, backups, tokens, passwords, or real scanner configuration.
- Before making this repository public or deploying a change, review the code and staged Git diff for hardcoded secrets and unsafe defaults.
- Login/security settings and behavior can change; verify the current configuration in `app.py`, `docker-compose.yml`, and `.env.example`.

## Backups and upgrades

Back up `data/` before updates, migrations, or experimentation. The included backup service creates scheduled compressed snapshots, but backups on the same disk are not protection against disk failure. Keep an additional copy somewhere separate and periodically test restoring it.

To update code after backing up:

```bash
git pull
docker compose up -d --build
docker compose logs --tail=100 web
```

Do not assume every update is backward-compatible. Review commits and release notes before applying updates to a live inventory.

## Local development preview (no GitHub publishing required)

On Linux with Fish installed, run the web app directly from your working tree:

```fish
./dev.fish
```

The first run creates `.venv/`, installs `requirements.txt`, and makes an isolated `data-dev/` snapshot from `data/` (if present). The development server is available at `http://127.0.0.1:5050`, uses Flask's debug reloader, and watches your local Python, template, and CSS changes. Save a file and refresh the browser; there is no need to commit, push, or rebuild the production container.

The helper uses SQLite's backup API when copying `.db` files so committed WAL data is included. It intentionally does not copy scheduled backups. The ZIP/repository does not include private runtime data, so when there is no local `data/` folder, point it at a safe copy of the server's data directory:

```fish
env CARD_SCANNER_DEV_SOURCE=/path/to/a/safe/data-copy ./dev.fish --reset-data
```

The development server still uses a separate `data-dev/` snapshot and never writes to the source directory. The first snapshot is reused on later runs; after updating the source backup, refresh the isolated copy with:

```fish
./dev.fish --reset-data
```

When the source database already has users, sign in with the copied account. A brand-new empty development database bootstraps an `admin` user with password `local-dev-only-password-change-me`. That password is for the loopback-only development copy; never reuse it elsewhere. The dev server binds only to `127.0.0.1`; do not expose this debug server publicly. Remove `data-dev/` when you no longer need the local snapshot.

Run the automated regression suite from the virtual environment with:

```fish
.venv/bin/python -m unittest discover -s tests -v
```

## Development and collaboration

- Keep web application code and scanner-client code in their respective areas.
- Use feature branches for changes and pull requests to merge into `main`.
- Avoid committing generated data, local configuration, caches, logs, or backups.
- Test database migrations and scanner API changes against a backup or test instance before using them with the live collection.

## Data sources and third-party services

This project integrates with or relies on services/data from TCGCSV, Scryfall, and Archidekt, and can optionally send notifications to Discord. Availability, formats, terms, and API behavior can change independently of this project. This is an unofficial community project and is not affiliated with or endorsed by Wizards of the Coast, Scryfall, TCGCSV, Archidekt, or Discord.

Magic: The Gathering is a trademark of Wizards of the Coast LLC. Card names, images, and related game data belong to their respective owners.


## Mobile scanner v39.3

The mobile scanner searches the local `data/mtg_art_hashes.db` catalog by card name, set code, and collector number. Mobile prices come from the local TCGCSV snapshot. Mobile search and price lookup do not call the Scryfall API. Static printing thumbnails may still be loaded from Scryfall image CDN URLs. Camera capture requests continuous autofocus when supported and chooses the sharpest frame available from a short capture burst. Title OCR detects the card border and crops its title relative to the card, with thresholded and multi-position fallbacks.


**v39.3 mobile OCR fix:** Detects the card rectangle before OCR instead of cropping a fixed region at the top of the entire camera frame. Adds confidence-aware thresholded crops and a vertical multi-crop fallback for nonstandard framing. See `scanner-client/CHANGES_scanner-mobile_v39.3.md`.


**v39.4 mobile scanner:** reads the card footer for set/collector number from the local card catalog, normalizes leading zeros, corrects a stray leading OCR glyph only when the original title has no local matches, and matches saved captures to live-view native zoom on Android devices where `takePhoto()` returns an unzoomed image. See `scanner-client/CHANGES_scanner-mobile_v39.4.md`.


**v39.4:** Mobile capture avoids Android `ImageCapture.takePhoto()` when native zoom is active, because some devices return a full-frame still that ignores live-preview zoom. It also OCRs the card footer for collector number/set-code clues and uses only the local printing catalog to narrow choices. If title OCR adds a stray leading glyph, the local search retries without it only when the full text has no matches. See `scanner-client/CHANGES_scanner-mobile_v39.4.md`.


## Mobile scanner v39.11

The scanner now corroborates a local set/collector match with the card title before showing one-tap confirmation. If title and footer disagree, it ignores the footer guess and searches by title; if title OCR is weak, it shows footer candidates for manual review without one-tap confirmation. See `scanner-client/CHANGES_scanner-mobile_v39.11.md`.
