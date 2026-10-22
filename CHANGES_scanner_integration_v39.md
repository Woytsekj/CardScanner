# CardScanner v39 integration notes

This update combines the v39 scanner-client changes with the main Flask endpoints required to use the client and Android PWA.

## Main Flask application

- Added `GET /api/scanner/options` for the trusted desktop scanner key, returning active people with mapped account usernames and known storage-location labels.
- Extended `/api/scan` and batch scan writes to validate optional `person_id` / `account_id`, carry storage location independently, and persist the separate `for_vending` field from `is_vending` / `vending` aliases.
- Scan deduplication now accounts for location and vending status, so copies in separate physical/vending buckets do not merge. Stable `scan_id` requests are idempotent.
- Added a same-origin installable PWA at `/scanner/mobile/` and session-authenticated APIs for mobile options, price lookup, and scan saves.
- Injects a session-specific CSRF token into the served PWA HTML, disables caching of authenticated HTML/API data, and scopes camera permission plus required Tesseract/Scryfall CSP allowances to the mobile page.
- Mobile scan endpoints authorize the selected person server-side. Non-admin accounts are restricted to their single `users.person_id` mapping; admin accounts may choose any active person. Vendor-only accounts cannot access the scanner.

## Client

- The PySide6 desktop scanner keeps its existing card detection interface, adds location/vending controls, account-aware person choices, stable scan IDs, and improved validation/rejected-queue handling.
- Added an Android-friendly PWA using the phone's built-in camera, OCR name suggestion, and exact Scryfall printing selection. It does not embed a scanner key or Cloudflare service-token secret.
- The Fish launcher defaults to the desktop camera. The old scrcpy phone-as-PC-camera workflow remains optional.

## Schema and deployment notes

- The existing database schema already contains `inventory.location`, `inventory.deck_id`, and `inventory.for_vending`; no new inventory schema is required by these scanner routes.
- Physical locations remain free-form text labels because the app has no dedicated location table. Mobile location options are collected from existing inventory labels plus `Free`; both desktop and mobile clients also permit a new free-form location label.
- The current account model associates at most one default person with each login account. This release enforces that model rather than adding a many-to-many mapping implicitly.
- Prefer Cloudflare Access protection for the full public hostname. With path-scoped applications, explicitly cover `/scanner/mobile`, `/scanner/mobile/*`, and `/api/scanner/mobile/*`; each friend still requires a CardScanner login mapped to an active person.

## Validation and remaining checks

- Python syntax compilation and JavaScript syntax checks passed.
- All six scanner-client regression tests passed.
- The Flask route suite could not be executed in the artifact runtime because Flask was absent and package-index access was unavailable. New route regression tests were added in `tests/test_scanner_mobile_api.py`; run the repository's `python -m unittest discover -s tests -t . -p 'test_*.py' -v` command in its normal dependency/container environment.
- The desktop GUI camera and Android install/camera flow still need device testing. Cloudflare Access policy behavior must be verified in the actual deployment.
