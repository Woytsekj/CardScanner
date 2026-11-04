# CardScanner v39.12 — Deck allocation, finish-aware imports, and fuzzy matching

This patch is built against the exact source snapshot uploaded from the current `main` working tree. It is not based on the older Blueprint/routes layout.

## Deck ownership and allocation

- A one-time database migration creates/reactivates the existing person `Jonathan` and assigns every existing deck to that person. A corrective migration also retires the mistaken `People Jonathan` alias when no inventory rows use it. It does not modify `inventory.owner`, inventory quantities, locations, deck assignments, or price history.
- Deck settings are moved near the deck header and the owner field is required to select one active person.
- The deck owner is authoritative for inventory comparison; the old separate comparison-owner selector is removed.
- Deck requirements show every matching inventory source in priority order: green (already in this deck), yellow (owner's free pool), blue (owner's other deck), purple (another person's copy), and red (no matching copy exists in tracked inventory).
- Source details include quantity, owner, associated deck/free pool, storage location, finish, set, and collector number.
- Blue and purple copies are included before the red buy count. Jonathan's blue copies count as owned for value totals; purple copies do not become Jonathan-owned.
- Moving a blue copy prompts for confirmation. The endpoint verifies the card is actually required by the destination deck and belongs to that deck's owner. A full-quantity move changes the allocation; a partial move splits the inventory row. Owner, finish, printing, and physical location are preserved. The previous deck's `deck_cards` requirement is not deleted, so that deck now shows the moved copy as blue.
- Purple cards cannot be moved automatically and never have their ownership changed by this flow.

## Imports and pricing

- ManaBox/collection CSV imports now honor an explicit `Foil` flag before a generic `Finish` field, including explicit `true` and `false` values.
- Archidekt, Moxfield, and deck CSV finish parsing honor explicit boolean/string foil values. Archidekt modifier lists can also identify foil.
- Import merge keys include printing and finish so normal and foil copies, or distinct printings, are not collapsed together.
- Deck price summaries continue to show all prices that are available and label the result as partial when other copies lack metadata/prices. A priced `$0.00` entry is still recognized as priced data.
- Fuzzy matching compares Oracle IDs when both sides have them. If either side lacks Oracle metadata, it falls back to normalized card-name matching, which addresses missing metadata such as the Psychosis Crawler case.

## Data safety

No database file is included. No database reset is performed. Pre-existing inventory rows with an incorrectly stored finish are not automatically rewritten; the importer fix applies to new imports and avoids guessing the physical finish of already stored cards.

## Verification

- `python -m py_compile app.py tests/test_deck_redesign.py tests/test_deck_pricing.py` passed in the build environment.
- Jinja parsing of `templates/deck_view.html` passed.
- Isolated tests passed for Archidekt/Moxfield/deck CSV finish parsing, ManaBox foil-flag logic, fuzzy name fallback, and partial pricing (including `$0.00`).
- SQLite logic checks passed for source ordering, stock accounting, and a moved copy appearing blue in its previous deck.
- The full Flask route/database test suite was not run in the build environment because Flask is not installed there. The added `tests/test_deck_redesign.py` includes route/migration coverage to run in the project's normal virtual environment.
