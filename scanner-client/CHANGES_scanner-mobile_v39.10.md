# CardScanner Mobile v39.10 — one-tap unique-match confirmation

## Changes
- Adds a compact sticky confirmation bar near the top of the viewport when the local printing search returns exactly one result.
- The bar shows the card name, set code, and collector number, with a **Yes — use this card** action.
- Tapping Yes selects the unique printing and enables the existing sticky **Save & next** control. It does not save automatically, preserving an explicit inventory-write action.
- When there are zero or multiple results, the quick Yes bar remains hidden; the user can scroll to review and select the candidate list.
- The quick bar disappears after a candidate is selected or a new search begins.
- Bumps the mobile service-worker cache so the updated scanner assets reload.

## Validation
- Mobile UI regression assertions cover the quick-confirm element, unique-result condition, and select-only handler.
- Test on an Android device to verify the sticky top action remains reachable while the camera/candidate list is scrolled.
