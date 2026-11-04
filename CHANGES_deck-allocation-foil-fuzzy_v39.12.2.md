# CardScanner v39.12.2 — Fuzzy printing assignment and generic basic lands

## Fuzzy assignment uses the selected physical printing

- When fuzzy mode finds a same-Oracle card with a different printing and the user assigns/moves that inventory copy into the deck, the selected deck requirement is updated to the actual inventory printing. Generic basic-land requirements also update to the selected actual printing when assigned, even with fuzzy mode off.
- The deck requirement's Scryfall ID, set, collector number, finish, and TCGplayer mapping are updated from the chosen copy where available. The deck's card image therefore follows the physical printing the user selected.
- Generic basic-land requirements update their displayed art to the first/most recently selected physical printing when fuzzy mode is used, while their `Any artwork / printing` flag remains enabled.
- The inventory row remains the physical source of truth; its owner, quantity, finish, set/collector data, and storage location are preserved by the existing move behavior.

## Generic basic lands

- Plains, Island, Swamp, Mountain, Forest, Wastes, and the five Snow-Covered basics are automatically marked as `Any artwork / printing` when decks are imported or cards are manually added.
- Existing deck entries for those basic land names are marked generic by a one-time, idempotent migration.
- A generic land requirement matches the same land name across different Scryfall printings/art without needing global fuzzy mode, while always respecting normal versus foil finish.
- Inventory source rows still show each actual owned printing and its location, so the user can choose the physical copies to assign. The requirement continues to match all art variants of that same named basic land after its displayed image changes.
- The Edit card dialog exposes the `Match any artwork/printing` option, allowing a generic requirement to be made printing-specific, or another requirement to be made generic.

## Data safety

- Fuzzy matching changes art/printing identity only; foil and nonfoil remain distinct so finish-specific pricing stays accurate.
- Adds `deck_cards.any_printing` with a default of `0`; existing non-basic entries are unchanged.
- Does not alter inventory ownership, quantities, prices, price history, physical locations, or card scanning data.
- No database reset is required.
