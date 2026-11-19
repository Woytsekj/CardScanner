-- Initial PostgreSQL foundation: namespaces, stable game IDs, and provider registry.
-- This migration deliberately does not alter or migrate the existing SQLite data.

CREATE SCHEMA IF NOT EXISTS app;
CREATE SCHEMA IF NOT EXISTS catalog;

CREATE TABLE IF NOT EXISTS catalog.games (
    game_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT games_slug_format CHECK (game_id ~ '^[a-z][a-z0-9_]*$')
);

CREATE TABLE IF NOT EXISTS catalog.providers (
    provider_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    capabilities TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    homepage_url TEXT,
    is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT providers_slug_format CHECK (provider_id ~ '^[a-z][a-z0-9_]*$')
);

CREATE TABLE IF NOT EXISTS catalog.game_providers (
    game_id TEXT NOT NULL REFERENCES catalog.games(game_id) ON DELETE CASCADE,
    provider_id TEXT NOT NULL REFERENCES catalog.providers(provider_id) ON DELETE RESTRICT,
    provider_category_id TEXT,
    is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    config JSONB NOT NULL DEFAULT '{}'::JSONB,
    last_attempted_at TIMESTAMPTZ,
    last_successful_at TIMESTAMPTZ,
    last_source_updated_at TIMESTAMPTZ,
    last_status TEXT NOT NULL DEFAULT 'never',
    last_error TEXT,
    last_item_count BIGINT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (game_id, provider_id),
    CONSTRAINT game_provider_status CHECK (last_status IN ('never', 'running', 'success', 'skipped', 'error'))
);

CREATE INDEX IF NOT EXISTS game_providers_provider_idx
    ON catalog.game_providers(provider_id, is_enabled);

INSERT INTO catalog.games (game_id, display_name) VALUES
    ('mtg', 'Magic: The Gathering'),
    ('pokemon', 'Pokémon'),
    ('one_piece', 'One Piece Card Game'),
    ('riftbound', 'Riftbound')
ON CONFLICT (game_id) DO UPDATE
SET display_name = EXCLUDED.display_name;

INSERT INTO catalog.providers (provider_id, display_name, capabilities, homepage_url) VALUES
    ('tcgcsv', 'TCGCSV', ARRAY['product_catalog', 'pricing'], 'https://tcgcsv.com/'),
    ('scryfall', 'Scryfall', ARRAY['catalog', 'metadata', 'images'], 'https://scryfall.com/')
ON CONFLICT (provider_id) DO UPDATE
SET display_name = EXCLUDED.display_name,
    capabilities = EXCLUDED.capabilities,
    homepage_url = EXCLUDED.homepage_url;

INSERT INTO catalog.game_providers (game_id, provider_id, provider_category_id, config)
VALUES
    ('mtg', 'tcgcsv', '1', '{"category_name":"Magic: The Gathering"}'::JSONB),
    ('pokemon', 'tcgcsv', '3', '{"category_name":"Pokemon"}'::JSONB),
    ('one_piece', 'tcgcsv', '68', '{"category_name":"One Piece Card Game"}'::JSONB),
    ('riftbound', 'tcgcsv', '89', '{"category_name":"Riftbound League of Legends Trading Card Game"}'::JSONB),
    ('mtg', 'scryfall', NULL, '{"role":"catalog"}'::JSONB)
ON CONFLICT (game_id, provider_id) DO UPDATE
SET provider_category_id = EXCLUDED.provider_category_id,
    config = EXCLUDED.config,
    updated_at = now();
