# CardScanner Mobile v39.5 — desktop-style local OCR

This follow-up is intended for local development first. It keeps card-printing and price lookup local and makes the mobile recognizer use the same OCR family and local fuzzy-name matching strategy as the desktop scanner.

## Changes

- Adds authenticated `POST /api/scanner/mobile/recognize`, accepting a cropped card image and running EasyOCR locally on the CardScanner server.
- Uses original, contrast-normalized grayscale, and Otsu title crops; ranks OCR text against distinct names from the local `mtg_art_hashes.db` catalog using WRatio, following desktop scanner behavior.
- Reads the card footer with EasyOCR too. When local set code and collector number identify a printing, the API returns that name as a suggestion; saving still requires user confirmation.
- Adds fuzzy local catalog correction for common OCR misspellings when SQL contains-search returns no result.
- Allows exact set-code plus collector-number local lookup without requiring a readable title, so footer metadata can rescue a badly read title.
- The endpoint makes no Scryfall API calls. The first local EasyOCR use may need to download EasyOCR model weights if they are not already cached; subsequent recognition runs locally.
- Browser Tesseract remains a fallback if the local OCR endpoint is unavailable.

## Testing

Added tests for fuzzy local correction, set/collector recovery when title OCR is unusable, the EasyOCR endpoint, and the mobile page integration. Run the full Flask test suite and test multiple real cards on the local development instance before deploying.
