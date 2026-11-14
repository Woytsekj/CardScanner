# PostgreSQL and Multi-Game Migration Plan

Status: design baseline; no production changes authorized or performed by this document.

## Goals

- Move the server-side authoritative data from SQLite to PostgreSQL without changing current MTG behavior.
- Make the data model game-aware from the first PostgreSQL schema version.
- Keep the desktop scanner's local SQLite workflow and the existing MTG SQLite catalog download contract working during the transition.
- Use TCGCSV as the default price-data source for every supported game/category it provides, without introducing a paid pricing API dependency.
- Keep catalog providers, image/recognition providers, and pricing providers independent.
- Keep PostgreSQL's data directory on Bertha's local filesystem; keep large source downloads and PostgreSQL backup archives on the existing NAS data share.

## Initial game registry

Use stable internal slugs, not provider category IDs, as application identities:

| Slug | Display name | Initial TCGCSV category ID | Notes |
|---|---|---:|---|
| `mtg` | Magic: The Gathering | 1 | Existing production game; preserve behavior and Scryfall integration |
| `pokemon` | Pokémon | 3 | Initial TCGCSV product and price source |
| `one_piece` | One Piece Card Game | 68 | Initial TCGCSV product and price source |
| `riftbound` | Riftbound | 89 | Initial TCGCSV product and price source; Riot asset/API policy is a separate gate |

Category IDs are provider-specific configuration, not primary keys or game IDs. The updater must discover/validate categories from TCGCSV's categories response and must not assume that a category ID will never change. A missing or renamed category must disable that game's TCGCSV sync with a clear health/status error rather than silently syncing the wrong category.

## Architecture decisions

### PostgreSQL deployment

- One PostgreSQL database named `cardscanner`.
- Separate PostgreSQL schemas: `app` for users, inventory, decks, scan history, price history and application state; `catalog` for games, providers, sets, cards, printings, faces, provider identifiers, images and recognition hashes.
- Psycopg 3 for database access. Avoid a wholesale ORM rewrite.
- A small centralized database module owns connections, transactions, row handling and bounded pooling.
- Create pools after Gunicorn worker fork; never share live connections across forked workers.
- Versioned, ordered schema migrations. Migrations must be repeatable-safe and record applied versions.
- PostgreSQL data files live on Bertha local storage (for example `/var/lib/cardscanner/postgres`), never on NFS.
- Preserve the current NAS data directory for bulk source files, price snapshots during transition, sync metadata, and backup archives.

### Multi-game catalog model

Use normalized identities with provider-specific identifiers:

- `catalog.games`: internal game slug and display metadata.
- `catalog.providers`: provider identity and provider type (catalog, pricing, images, recognition).
- `catalog.game_providers`: enabled provider configuration per game, including provider-specific category IDs and sync state.
- `catalog.sets`: game-owned expansions/groups; retain source/provider set IDs as external identifiers.
- `catalog.cards`: game-level card identity where the provider can establish one.
- `catalog.printings`: a specific set/collector-number/language/finish/art printing. A printing belongs to one game and one card identity where known.
- `catalog.card_faces`: face/side metadata for multi-face or double-sided cards.
- `catalog.external_ids`: provider-scoped identifiers for cards, printings, sets and products. Do not treat Scryfall IDs or TCGplayer product IDs as universal IDs.
- `catalog.images` and `catalog.image_hashes`: image references and recognition hashes tied to the relevant printing/face and source.
- Provider-specific metadata may use JSONB where it is genuinely provider-specific; fields used for joins, constraints, matching, filtering or sorting should be normal columns.

The schema must not require every game to have Oracle identity, mana cost, rarity, collector-number format, faces, or a Scryfall-like provider. Unknown or unavailable metadata must remain nullable/optional.

### Inventory and decks

- Every inventory card and deck belongs to exactly one game.
- Every catalog printing belongs to exactly one game.
- Enforce game consistency between inventory/deck records and referenced cards/printings using relational constraints or an equivalent validated design; do not rely only on UI checks.
- Preserve current MTG ownership, person/account mapping, location, finish, vending, quantity, deck allocation and scan retry semantics.
- Do not assume TCGplayer product ID is the catalog primary key or a globally unique card identity.

### Pricing: TCGCSV first

TCGCSV is the default pricing provider for the four initial games when its category exists and contains relevant product data. Keep the pricing adapter replaceable.

TCGCSV product IDs are provider IDs. Store them in provider-scoped external-ID mappings. Price observations should retain at least:

- game and printing/product mapping;
- provider and provider product ID;
- source subtype/variant (for example Normal, Foil, Holofoil, Reverse Holofoil);
- observed price fields actually supplied by TCGCSV (market, low, mid, high as available);
- source update timestamp and ingestion timestamp.

Do not collapse different TCGCSV subtype names into one price. Map subtype names to normalized finish/variant values only when the mapping is known; retain the original source value regardless.

The updater must follow the current TCGCSV usage guidance:

1. Check `https://tcgcsv.com/last-updated.txt` before starting a sync.
2. Sync no more than once per daily data build; skip when the timestamp has not advanced beyond the last successful sync.
3. Use a descriptive CardScanner User-Agent.
4. Add at least 100 ms between requests and handle throttling, timeouts, and empty-group responses.
5. Keep TCGCSV requests server-side; the browser and desktop client read CardScanner's local data/API only.
6. Avoid downloading the full immutable historical price archive repeatedly. Daily current-price sync is the initial scope; archive backfill is a separate, explicitly planned task.
7. Record per-game/per-category sync status, last attempted timestamp, last successful source timestamp, row counts and errors.

Do not assume TCGCSV provides SKU-level condition pricing. Its product/price data is not a substitute for condition-specific SKU data.

### Scanner compatibility

- Keep the desktop scanner's local SQLite databases and offline workflow.
- Preserve the existing MTG catalog artifact name, SQLite schema, download endpoint, and API payload during the first PostgreSQL release.
- Generate the legacy `mtg_art_hashes.db` artifact from PostgreSQL catalog data and verify it with SQLite integrity/schema tests.
- Do not switch the existing MTG scanner to a new generic catalog format in the same cutover.
- A versioned game-aware SQLite catalog export can be introduced later for non-MTG games, with explicit client capability/version negotiation.
- Shared image processing/OCR/hash infrastructure can be reused, but card identification and metadata interpretation belong to game/provider adapters.

### Provider and rights boundaries

- Scryfall remains the MTG catalog/metadata provider; TCGCSV remains its pricing provider.
- TCGCSV pricing/product data does not automatically grant rights to reuse every game's artwork.
- Riftbound catalog assets and recognition integration remain gated on Riot's current developer/API and asset requirements. Do not substitute unofficial images or assume TCGCSV product images are approved for CardScanner's use.
- One Piece and Pokémon catalog/image providers are separate decisions from TCGCSV pricing.

## Migration phases

### Phase 0 — safety and baseline (current)

- Work on a dedicated feature branch from the audited `main` baseline.
- Do not change production, the Bertha Compose deployment, local database files, secrets, staged production-specific changes, stashes or backup files.
- Keep production's current SQLite database and backup archives untouched.
- Confirm the branch baseline and review the complete source before implementing schema changes.

### Phase 1 — PostgreSQL foundation

- Add PostgreSQL service/configuration for development and tests only.
- Add psycopg 3, centralized connection/transaction module, pool lifecycle safe for Gunicorn, and migration runner.
- Add health checks and a local development profile; do not enable it in production Compose yet.
- Add PostgreSQL integration tests to CI.

### Phase 2 — game-aware catalog and application schema

- Create the initial game/provider registry and normalized catalog tables.
- Add game ownership to inventory and deck relationships while preserving MTG defaults and existing behavior.
- Add provider-scoped identifiers and generic price observation tables.
- Validate constraints against a copy of production data before migrating real data.

### Phase 3 — MTG data migration and compatibility

- Migrate inventory/auth/decks/metadata/rulings/scan/price history and Scryfall catalog data.
- Resolve and report orphaned references before enabling foreign keys.
- Replace SQLite ATTACH-based cross-database joins with PostgreSQL joins within the single database.
- Preserve scanner API and legacy SQLite export behavior.
- Keep existing TCGCSV MTG price data/history and Scryfall synchronization working.

### Phase 4 — PostgreSQL verification

- Run unit tests and real PostgreSQL integration tests.
- Test Scryfall bulk/staging imports and pHash batches.
- Test scanner catalog export and SQLite integrity.
- Test current price sync, price history, alerts, decks, inventory, mobile APIs and concurrent Gunicorn access.
- Test `pg_dump -Fc` and an actual `pg_restore` into a clean database.

### Phase 5 — controlled production cutover

- Take and verify a fresh SQLite backup.
- Stop writers only during the planned cutover window.
- Migrate a final consistent snapshot and verify counts/checksums/integrity.
- Start PostgreSQL-backed services only after preflight checks pass.
- Retain original SQLite files and backups unchanged.
- Rollback must account for writes made after cutover: stop all PostgreSQL writers and export the current PostgreSQL state into a fresh SQLite fallback before switching back. The pre-cutover SQLite snapshot alone is not a valid rollback for post-cutover writes.

### Phase 6 — additional game integrations

- Add Pokémon and One Piece catalog/image adapters and game-aware local scanner support.
- Add Riftbound only after confirming Riot's current app registration, API and asset requirements.
- Enable TCGCSV pricing for each game independently after validating category/product mappings and finish/subtype behavior.

## Explicit non-goals for the first PostgreSQL cutover

- No full scanner rewrite.
- No new game catalog ingestion or game-specific OCR rollout.
- No mandatory paid API.
- No migration of file-based Scryfall bulk downloads, deck cover images, JSONL queues or other unrelated files into PostgreSQL.
- No deletion of SQLite source databases or old backups.
- No production deployment as part of development commits.

## TCGCSV references

- Documentation and usage guidelines: https://tcgcsv.com/docs
- FAQ and price archive notes: https://tcgcsv.com/faq
- Categories: https://tcgcsv.com/tcgplayer/categories
- One Piece category 68, Romance Dawn group 3188: https://tcgcsv.com/tcgplayer/68/3188/products
