# CardScanner Mobile v39.11 — title-corroborated metadata

## Why
A single OCR read of the collector footer could produce a valid-looking set/number pair for a different card. The client then treated one local result as definitive and displayed the one-tap confirmation without reading the title.

## Changes
- Footer metadata is now an early clue, not a sufficient standalone reason to auto-confirm a card.
- The mobile client always reads the title before accepting a unique footer candidate. A strong title disagreement discards the OCR set/collector pair and searches the local catalog by title instead.
- If the title OCR is too weak to corroborate or reject footer metadata, the scanner may display local footer candidates for manual selection, but it suppresses the one-tap Yes confirmation.
- Local EasyOCR fallback now reads both footer and title even when the footer query returns one result. It returns metadata as verified only when the independently recognized title agrees with that catalog card.
- The local search endpoint no longer falls back to a footer-only result when a non-empty title query conflicts; footer-only recovery remains available when the client explicitly has no usable title.
- Added regression coverage for a wrong unique footer match (e.g. `FRA #367` returning a different card than the visible title).
- Bumped the PWA service-worker cache so Android browsers fetch the updated scanner JavaScript.

## Performance / behavior
The browser reuses its existing OCR worker and does one title crop after the footer pass. This adds one lightweight title check to unique-footer scans, intentionally trading a small amount of latency for protection against confidently wrong card selection. There are still no live Scryfall search calls.
