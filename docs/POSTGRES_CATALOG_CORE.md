# PostgreSQL catalog core (migration 0002)

Status: development schema only. This migration is additive and does not import,
alter, or delete data in either production SQLite database.

Apply with the repository migration runner against a disposable PostgreSQL
database only. Do not add the development PostgreSQL service to the production
Compose project.

## Identity model

- `catalog.games` remains the source of internal game identities.
- `catalog.sets` represents game-scoped expansions/releases. Set code is
  optional and is unique case-insensitively only within a game when present.
- `catalog.cards` represents a canonical game-level card when a source can
  establish one. Name is required; provider identifiers remain external IDs.
- `catalog.printings` represents a specific game-specific printing. Card and
  set references are optional because providers/games may not expose a stable
  canonical identity or set mapping. Collector numbers are TEXT and are not
  globally unique: variants and languages can share one.
- `catalog.printing_faces` stores zero or more ordered faces for a printing.
- `catalog.illustrations` represents an artwork identity independently of a
  printing or face. This is intentional: the existing MTG artwork catalog has
  tag assignments for nine illustration IDs that currently have no matching
  `card_art` or `card_art_faces` illustration reference. Future import code
  must preserve those IDs and assignments rather than fabricate faces or drop
  assignments.
- `catalog.external_ids` maps provider-scoped identifiers to exactly one
  game-scoped card, printing, set, or illustration. The external identifier is
  not an internal primary key. `external_id_type` distinguishes namespaces
  such as Scryfall's printing ID and Oracle ID. Product identifiers belong in
  the provider-product model planned for a later migration, not in this table.

The composite foreign keys include `game_id`; they prevent a printing from
referencing a card, set, image, or illustration belonging to another game.
Nullable card/set/face references remain optional. Deleting an image clears
only the optional `image_id`, retaining the required `game_id`.

## Images and recognition hashes

- `catalog.images` stores a provider-sourced image resource, uniquely scoped
  by game, provider, and URI. A changed URI is a distinct resource.
- `catalog.image_hashes` stores algorithm- and version-specific hashes for an
  image resource. Hashes are not attached directly to a printing, so they
  cannot accidentally survive a change to the image resource.
- The initial schema supports pHash and other future algorithms through
  `hash_algorithm` and `hash_version`; hash values are retained as TEXT to
  support existing hexadecimal pHashes and future encodings.

## Cache revision

`catalog.game_catalog_state` maintains one monotonically increasing revision
per game. Catalog importers must increment the affected game's revision in the
same transaction as a successful catalog refresh. Application caches can key
off this value instead of relying on SQLite file modification time and size.
The migration seeds a revision row for each game already in `catalog.games`.

## Deliberate scope boundaries

Migration 0002 does not create the Scryfall tag system, TCGCSV provider products,
product-to-printing mappings, prices, inventory, users, decks, or application
metadata. Those belong to later migrations.

It does not assume every game has:
- an Oracle identity;
- a Scryfall-compatible provider;
- mana cost, rarity, rules text, or faces;
- a stable collector-number format;
- artwork or a permitted image source.

Provider-specific fields may be stored in JSONB, but identity and relationship
fields used for joins and constraints remain relational.

## Tests

The PostgreSQL integration tests verify:
- all catalog-core tables exist;
- catalog rows can be created for MTG;
- an illustration can exist without a currently known face;
- printings can be recorded without a resolved canonical card or set;
- a Pokémon card cannot be attached to an MTG printing;
- a provider identifier cannot be assigned twice in the same game/provider/type
  namespace;
- image deletion clears optional image references without nulling game scope,
  and removes dependent image hashes;
- the migration runner remains idempotent.

All fixtures run inside a transaction that is rolled back. These tests do not
read or write the production SQLite databases.
