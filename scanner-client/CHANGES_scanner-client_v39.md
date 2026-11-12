# CardScanner v39 — scanner integration change summary

## Desktop scanner

- Added a storage-location selector and an independent **Mark for vending** checkbox.
- Person options now retain the server's person ID and current linked-account username; the UI shows the mapped account when available.
- Scanner requests include `person_id`, `account_id`, `location`, `location_id`, `storage_location`, vending metadata, and a stable scan ID for safe retries.
- Added matching desktop options route and server validation/persistence in the main Flask application. Physical location, deck assignment, and vending status remain independent.
- Local fallback inventory tracks account/person, location, and vending separately.
- 4xx validation/authorization failures are shown as rejected scans rather than being treated as offline saves. Later queue rejections are preserved in `scan_queue_rejected.jsonl` and do not block later queued scans.
- The Fish launcher defaults to the local desktop camera instead of automatically starting `scrcpy`; set `MTG_SCRCPY_CAMERA=1` to opt into the legacy phone-as-PC-camera workflow.

## Android PWA

- Added `mobile-app/`, an installable same-origin PWA at `/scanner/mobile/`.
- Uses the phone camera directly, with Tesseract.js title OCR as a suggestion, manual name correction, Scryfall printing selection, finish/quantity, person, location, and vending options.
- Uses the CardScanner login session plus CSRF validation; does not ship the scanner API key or a Cloudflare service-token secret to the browser.
- The PWA caches only static assets; authenticated HTML containing a CSRF token and all API/inventory responses are not cached.
- The mobile routes are implemented in `app.py`, including person authorization, price lookup, and safe scan writes.

## Important account-model limitation

The current main app's schema maps each web login to at most one default person through `users.person_id`. This release scopes non-admin mobile access to that person. Supporting multiple people under a single login will require a deliberate one-to-many account/people schema migration and admin UI change; it is not inferred from the current schema.

## Validation

- Python syntax compilation passed for the edited Flask app, scanner modules, and tests.
- The environment used for this artifact did not have Flask installed and could not access PyPI, so the Flask route/test suite could not be executed here. The new regression tests are included under `tests/test_scanner_mobile_api.py` and should run in the repository's normal CI/container environment.
- Desktop GUI camera flow and Android camera/PWA install still require device-level testing. No live Cloudflare deployment was available to verify Access policy configuration end-to-end.


## Production data synchronization

- Added scanner-key-authenticated server endpoints for a small client-data manifest and allow-listed downloads of `mtg_art_hashes.db` and `tcg_prices.json`.
- Art-catalog downloads use a consistent SQLite backup snapshot, including committed WAL data. The server reports source modification time, printing count, non-empty image-hash count, byte size, and SHA256.
- Desktop sync compares hashes and downloads only changed files. It stages files beside the local copy, validates SHA256 plus SQLite integrity/schema or TCGCSV JSON structure, then atomically replaces the local file.
- Added automatic synchronization at startup (configurable with `MTG_SCANNER_SYNC_ON_STARTUP`) and a **Sync Server Data** button for manual refresh.
- Failed or incomplete downloads leave the last valid local data intact. The scanner continues to use its local copies when the production server is unavailable.
- The existing TCGCSV updater continues its hourly checks. This change distributes the current production art-hash database; it does not create an art-hash builder or schedule catalog regeneration. The current repository still has no automatic art-hash generation job.
