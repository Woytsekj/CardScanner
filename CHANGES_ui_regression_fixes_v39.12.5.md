# CardScanner v39.12.5 — UI regression fixes

- Decks created by older/manual paths with a NULL owner now receive the signed-in account's linked Person when available; otherwise Jonathan is preferred, followed by the first active Person.
- The deck header, owner summary, and owner selector all display the effective deck owner consistently.
- The mobile PWA retains its own scanner release label (`v39.11`) instead of inheriting the web application's version number. This keeps the scanner client release independent from web UI hotfix releases.

No inventory ownership, card quantities, storage locations, price history, or scanner client behavior is changed.
