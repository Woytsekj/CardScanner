# CardScanner Mobile v39.6 — metadata-first, faster OCR

## Recognition changes

- Read the bottom footer first, where the collector number and printed set code usually appear.
- When local catalog data resolves an exact set-code + collector-number pair to one printing, use the catalog name as the title guidance and skip title OCR.
- When metadata is incomplete or ambiguous, use the title as supporting evidence and constrain local printing search with any detected set/collector values.
- Permit a metadata-only local search only when both set code and collector number were recovered. It cannot save a scan automatically; the user must still select the printing and save it.
- Cache the local set-code vocabulary by database path/mtime/size so footer parsing doesn't scan all distinct set codes for every card.

## Speed improvements

- Reuse a single Tesseract.js worker rather than creating and tearing down an OCR worker for every crop.
- Read the footer first and skip title OCR for a unique exact metadata match.
- Reduce normal browser OCR to one footer pass and one title pass, with one alternate title crop only for low-confidence results.
- Warm the browser OCR worker when starting the camera, while the user frames a card.
- Coarsen the low-resolution card-border search grid to reduce JavaScript work on phones.
- Server-side EasyOCR fallback now reads metadata first, returns after one pass for a unique set/collector match, and otherwise uses one title pass instead of testing three title variants on every request.
- Added processing time and OCR-pass fields to the optional local EasyOCR endpoint for diagnostics.

## Set symbol note

This release prioritizes printed set-code text plus collector number. Some older/alternate cards show only a set symbol, not a text set code. True symbol-only recognition is not enabled yet; it needs a local set-symbol template/reference library and image matching. No Scryfall search is added to the scan path.

## Testing

- Added regression tests for metadata-only search and a single-pass server fast path.
- `app.py` compiles and the inline mobile JavaScript passes `node --check` in the build environment.
- The full Flask test suite could not be executed in the build environment because Flask is not installed and package downloads are unavailable; run both test suites in the local `.venv` before merging.
