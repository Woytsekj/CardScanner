# CardScanner Mobile v39.8 — batch capture workflow

## Changes
- Added a fixed, safe-area-aware mobile save dock that appears when a printing is selected, so Save does not require scrolling to the bottom of the inventory form.
- Added a “Save & next” action for continuous mode and “Save card” when continuous mode is off.
- Reuses the existing authenticated save handler and CSRF-aware API call; no additional endpoint or browser-held credential was introduced.
- After a successful save in continuous mode, clears the selected printing and scan fields, keeps the camera active, and scrolls the capture area back into view for the next card.
- Preserves the selected person, location, and finish between scans; quantity resets to 1 and vending status resets to unchecked.
- Bumped the service-worker cache so the PWA fetches the updated client.

## Validation
- Python test assertion added for the sticky-save markup and event flow.
- JavaScript syntax and Python compilation should be rerun in the local development environment.


## v39.8.1 correction — batch vending mode

- Vending status is no longer cleared after a successful save. It remains selected for the current scan batch while quantity resets to 1.
- Added a vending toggle and explicit on/off state to the sticky dock, available during an active camera session. The main form checkbox and dock checkbox stay synchronized.
- Updated guidance to say the setting applies to every card until manually changed.
- The vending mode is session-level and resets when the page is reloaded, reducing the risk of accidentally carrying a vending setting into a later deck batch.
