# CardScanner Mobile v39.8.2 — compact prediction banner

## Changes
- Removed the large “CardScanner Mobile” header and subtitle to reclaim portrait screen space.
- Repurposed the green information panel as a compact scanner prediction banner: the headline shows “Scanner thinks: [card name]” after recognition, with footer/set/collector or confidence/timing details beneath.
- Errors stay visible in the same panel with a red warning style; progress and ready states stay compact.
- Metadata-first recognition only short-circuits title OCR when the local catalog returns exactly one printing. Multiple or ambiguous footer matches continue to title recognition instead of prematurely claiming a match.
- Includes v39.8.1’s sticky Save & next button and batch-persistent vending setting; quantity resets after each successful save.
- Bumped the service-worker cache name to force updated static assets to load.

## Validation
- Updated the mobile page regression assertions for the compact prediction UI and uniqueness requirement for the metadata fast path.
- Run the Python test suites and test on a real Android browser before deployment.
