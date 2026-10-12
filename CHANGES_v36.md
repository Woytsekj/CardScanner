# v36 changes (from v35)

Performance
- `get_tcg_prices()` caches the parsed price snapshot and reloads only when the file changes.
- `ensure_people_and_auth()` runs once per process; `current_user()` is resolved once per request.
- `/` no longer re-runs `migrate_db()` and no longer queries the full inventory twice when unfiltered.

Correctness
- `/api/scan` accepts an optional `scan_id`; a retried scan with the same id is acknowledged
  (`"duplicate": true`) without incrementing quantity. Old scanners that omit it behave as before.
- `/update` validates the row id and returns 404 for missing rows.
- CSV import skips rows whose owner is not an active person and reports them (`unknown_owners`).

Hardening
- API errors no longer include internal exception text.
- Removed unused Cloudflare Turnstile allowances from the CSP.
- `/vending` and `/vending/export.csv` have explicit auth decorators.
- Optional non-root containers via `APP_UID` / `APP_GID` (default unchanged).

Access is unchanged: LAN `http://SERVER-IP:5000` and the Cloudflare tunnel both work.


## Nightly database backups

- Added a dedicated backup container that creates a consistent SQLite backup every night.
- Backs up both `mtg_inventory.db` and `mtg_art_hashes.db`.
- Archives are written to `data/backups/` and the newest two are retained.
- Default schedule is 2:00 AM `America/New_York`; all settings are configurable in `.env`.
- SQLite's backup API is used so WAL-mode changes are captured safely.
