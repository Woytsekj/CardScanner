# CardScanner Mobile v39.1

## Improvements

- Added camera zoom slider and +/- controls. Devices with a supported camera zoom use the camera track's zoom capability; others use a digital crop fallback. Captured images use the same crop the user sees in the preview.
- Added Continuous scan mode (enabled by default). Capturing a card leaves the camera active, starts OCR automatically, and searches for matching printings; after a successful save the camera remains ready for the next card. Each capture still requires the user to confirm the exact printing and explicitly save it.
- Added collector-number parsing and an optional set-code filter. Queries such as `Forest 304 C21` are translated to Scryfall `name`, `cn`, and `set` search terms. Search results show set name, set code, collector number, rarity, and reference prices for printing confirmation.
- Bumped the app-reported version to `39.1` and the PWA service-worker cache to `cardscanner-mobile-v3` so browsers refresh the static shell.

## Validation

- Inline mobile JavaScript passes `node --check`.
- Search parser spot checks passed for `Forest 304 C21`, `Sol Ring 123`, and the separate set-code filter.
- Real-device zoom support varies by Android browser/camera. A digital crop fallback is used where track zoom is unavailable.
