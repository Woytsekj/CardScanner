# CardScanner Mobile v39.7

- Reduced the camera view's portrait height and shrank its guide so one physical card is easier to fit on screen.
- Footer OCR now crops the lower-left collector-info rows rather than the entire bottom of the card, reducing interference from rules text, the artist credit, and copyright numbers.
- Preserves OCR row boundaries and validates that a set code is paired with a language marker and a plausible collector number before treating the footer as definitive. A noisy result such as `REN 1` is rejected rather than allowed to select a wrong printing.
- The set and collector fields returned to the UI are exposed only when a local catalog result corroborates the pair.
- Bumped the mobile static-asset cache to v10 and app version to 39.7.

Reality Fracture cards such as Screeching Soulbreaker have printed set code `FRA` and collector number `65` (often rendered `0065`). The recognition path now looks for that specific footer structure. This change does not yet perform set-symbol image matching; if a card has no printed set code, title/local-catalog guidance and manual confirmation remain the fallback.
