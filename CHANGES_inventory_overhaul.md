# Inventory, deck, and vending update

## What changed

- Physical storage (`inventory.location`), deck membership (`inventory.deck_id`), and vending eligibility (`inventory.for_vending`) are separate fields.
- The one-time database migration marks legacy cards in `Free` for vending when `for_vending` is first introduced. Cards already assigned to a deck are not marked for vending. This is a one-time compatibility backfill: newly scanned, manually added, or imported cards in `Free` are not automatically put up for sale; explicitly mark them for vending. The migration does not re-enable a card that an administrator later opts out of vending.
- Legacy records whose old location text exactly matches a saved deck name are linked to that deck once. After the migration, changing physical storage to a string that happens to match a deck does not assign a card to that deck.
- Assigning a card to a deck clears its vending flag. Unassigning it does not silently opt it back into vending; an administrator can choose this with the individual toggle or bulk editor.
- Vending pages, CSV export, and labels use the vending flag instead of requiring the physical location to be `Free`.
- The inventory page has bulk selection and bulk editing for vending eligibility, storage location, and deck assignment. Filter by owner first (or use `owner:Name`), then select all currently matching rows.
- Search supports a local subset of Scryfall syntax: `set:`, `cn:`, `name:`, `t:`, `o:`, `c:`, `r:`, `is:foil`, `owner:`, `loc:`, `deck:`, `vending:`, `order:` and `direction:`. Metadata-based searches only match cards whose Scryfall metadata has been enriched.
- `order:set direction:asc` sorts by set and then by numeric collector number, where possible.
- Standard inventory CSV export/import now preserves the vending flag when that column is present.
- Automated regression tests cover legacy migration, one-time backfill, bulk vending operations across physical locations, deck assignment isolation, deck comparison, and Scryfall-style filtering/sorting.

## Suggested deployment

1. Back up the inventory database.
2. Merge the feature branch only after the required `docker-test` job passes.
3. Pull the merged `main` on the server and rebuild the web container: `docker compose -p mtg_scanner up -d --build web` (include `updater` if the updater image or its dependencies changed).
4. Open the inventory page and verify that legacy `Free` cards are marked for vending, then use the owner filter or `owner:FriendName` query to find a friend's cards, select all matching rows, choose **Mark for vending**, and apply.
5. Inspect several deck-assigned cards and confirm that assigning them to a deck removes them from vending while keeping their storage location unchanged.
