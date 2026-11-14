# PostgreSQL catalog tags (migration 0003)

Status: development schema only. This migration is additive and does not read,
import, alter, or delete production SQLite data.

## Scope

Migration `0003_catalog_tags.sql` creates provider- and game-scoped tag
structures:

- `catalog.tags`: provider tag identity, type, slug, label, description, and
  preserved source parent/child ID arrays.
- `catalog.tag_aliases`: case-insensitive aliases, unique per tag. The same
  alias may legitimately belong to more than one tag, matching the prior
  SQLite primary-key scope of (tag_id, alias).
- `catalog.tag_closure`: transitive ancestor/descendant relationships with
  source depth, constrained to one game and provider.
- `catalog.art_tag_assignments`: tag-to-illustration assignments.
- `catalog.oracle_tag_assignments`: tag-to-provider Oracle ID assignments,
  optionally linked to a resolved canonical card.

Internal tag and illustration primary keys are not source IDs. A Scryfall tag
ID is stored in `catalog.tags.external_tag_id`; a source illustration ID is
mapped to an internal `catalog.illustrations.illustration_id` using
`catalog.external_ids`.

## Preservation requirements for the MTG import

The production SQLite audit found:

- 16,451 Scryfall tags;
- 2,289 tag aliases;
- 46,256 tag-closure rows;
- 509,869 artwork-tag assignments;
- 240,245 Oracle-tag assignments.

The artwork assignments contain 30 rows referencing nine distinct illustration
IDs that currently have no matching illustration reference in
`card_art.illustration_id` or `card_art_faces.illustration_id`.

The importer must create an illustration identity for each of those source
IDs, record the Scryfall external identifier, and import every assignment.
It must not fabricate a printing face or drop an assignment to satisfy a
foreign key.

Oracle assignments retain the original external Oracle ID even when a matching
canonical `catalog.cards` row cannot be established. The nullable `card_id`
is an optional enrichment, not the authoritative source identity.

Weights and annotations remain source-preserving text. Parent and child source
ID arrays remain JSONB arrays while the normalized transitive closure supports
efficient ancestor/descendant queries.

## Transaction and refresh behavior

The current Scryfall updater stages the tag export and replaces the tags,
aliases, hierarchy, and both assignment collections in one SQLite transaction.
The PostgreSQL importer must preserve all-or-nothing visibility: load staging
data, validate provider/game relationships and row counts, then replace/upsert
the relevant provider/game slice and increment the affected catalog revision
within one transaction. A failed or empty source export must leave the previous
live dataset intact.

Do not delete or replace tags for unrelated games or providers during an MTG
refresh.

## Constraints and indexes

- Tag external IDs are unique within a game/provider.
- A tag's game/provider scope is enforced by composite foreign keys.
- Alias matching is case-insensitive per tag.
- Closure ancestor and descendant tags must share the declared game, provider, and tag type.
- Artwork assignments can reference only tags whose type is `art`; Oracle assignments can reference only tags whose type is `oracle`.
- Artwork assignments require an illustration identity in the same game, but
  do not require a printing face to exist.
- Oracle assignments retain an external Oracle ID and may optionally resolve
  to a same-game canonical card.
- Supporting indexes cover tag type/slug/label, alias lookup, hierarchy
  descendants, illustration assignment lookup, and Oracle ID/card lookup.

## Tests

The PostgreSQL integration tests cover:

- unresolved illustration identities with preserved art-tag assignments;
- original assignment weight and annotation preservation;
- case-insensitive alias uniqueness per tag while allowing aliases on multiple
  tags;
- game/provider-scoped hierarchy constraints;
- unresolved Oracle IDs without fabricated canonical cards;
- rejection of cross-game Oracle card links;
- the existing foundation and catalog-core integration behavior.

All test fixtures run inside rolled-back PostgreSQL transactions. No test reads
or writes either production SQLite database.
