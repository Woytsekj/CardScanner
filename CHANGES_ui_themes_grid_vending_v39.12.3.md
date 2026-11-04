# CardScanner v39.12.3 — UI themes, grid-first inventory and vending price history

## Changes
- Inventory now opens in Grid mode by default; users can still switch to List.
- Grid price cells flow as Scanned / Normal on the first row and Row / Foil on the second row. Normal and Foil use distinct accent treatments for quick comparison.
- Inventory pagination appears both above and below the results so mobile users can move pages without returning to the top.
- Added browser-local theme selection across Inventory, Vending, Decks, Deck View, Admin and Login: Dark, Midnight Blue, Forest and Light. The selected theme persists in that browser and does not affect other users.
- Vending cards with a TCGplayer ID now have a Price History dialog with 3-day through 1-year ranges, saved price stats and chart.
- Authorized vendor accounts may use the price-history API only for cards explicitly marked for vending; admins retain access for the full inventory.
- Cards without a TCGplayer product ID show why history cannot be opened.

## Data and safety
- No database schema change or data migration is required.
- No inventory, pricing, quantity, owner, deck or finish records are modified by this UI release.
- No database files, runtime data, backup files or production credentials are included. The existing scanner example configuration is included with blank values and obvious placeholders only.
