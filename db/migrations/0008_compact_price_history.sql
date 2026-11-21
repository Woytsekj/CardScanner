-- Compact, change-based provider price history.
--
-- The observation table becomes an event log: a row is written only when a
-- product/subtype price state changes (or becomes unavailable). Current state
-- is maintained separately so unchanged source builds can still refresh the
-- last-seen marker and source completeness can be reconciled.

ALTER TABLE catalog.provider_price_observations
    ADD COLUMN is_available BOOLEAN NOT NULL DEFAULT TRUE;

CREATE TABLE catalog.provider_price_current (
    game_id TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    external_product_id TEXT NOT NULL,
    subtype_name TEXT NOT NULL,
    source_updated TEXT NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL,
    market_price NUMERIC,
    low_price NUMERIC,
    mid_price NUMERIC,
    high_price NUMERIC,
    direct_low_price NUMERIC,
    is_available BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT provider_price_current_pk
        PRIMARY KEY (game_id, provider_id, external_product_id, subtype_name),
    CONSTRAINT provider_price_current_provider_fk
        FOREIGN KEY (game_id, provider_id)
        REFERENCES catalog.game_providers(game_id, provider_id)
        ON DELETE RESTRICT,
    CONSTRAINT provider_price_current_product_nonempty
        CHECK (btrim(external_product_id) <> ''),
    CONSTRAINT provider_price_current_subtype_nonempty
        CHECK (btrim(subtype_name) <> ''),
    CONSTRAINT provider_price_current_source_nonempty
        CHECK (btrim(source_updated) <> '')
);

CREATE INDEX catalog_provider_price_current_available_idx
    ON catalog.provider_price_current (
        game_id, provider_id, is_available, external_product_id
    );

-- Seed current state from the latest existing full-snapshot observation per
-- provider product/subtype. Existing history rows remain available as history.
INSERT INTO catalog.provider_price_current (
    game_id, provider_id, external_product_id, subtype_name,
    source_updated, captured_at, market_price, low_price, mid_price,
    high_price, direct_low_price, is_available
)
SELECT DISTINCT ON (
    game_id, provider_id, external_product_id, subtype_name
)
    game_id, provider_id, external_product_id, subtype_name,
    source_updated, captured_at, market_price, low_price, mid_price,
    high_price, direct_low_price, TRUE
FROM catalog.provider_price_observations
ORDER BY
    game_id, provider_id, external_product_id, subtype_name,
    captured_at DESC, source_updated DESC;

-- Every measured source_payload key is already represented by normalized
-- columns or the row identity/source-build fields. Avoid storing it twice.
ALTER TABLE catalog.provider_price_observations
    DROP COLUMN source_payload;

COMMENT ON TABLE catalog.provider_price_observations IS
    'Change events for provider price state, keyed by exact source subtype and source build. Values contain the full five-metric state; is_available=false is an explicit removal/unavailable event.';

COMMENT ON TABLE catalog.provider_price_current IS
    'Latest provider price state for every product/subtype, including last-seen source build and availability. This table is not the historical chart source.';
