-- Generic TCGCSV groups, products, and subtype-specific price observations.
--
-- Schema only. No production downloads or SQLite data are imported.
-- Provider IDs and provider timestamps remain source-preserving values.

-- Allow group rows to verify their provider's configured category.
ALTER TABLE catalog.game_providers
    ADD CONSTRAINT game_providers_category_scope_key
    UNIQUE (game_id, provider_id, provider_category_id);

CREATE TABLE catalog.provider_product_groups (
    game_id TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    external_category_id TEXT NOT NULL,
    external_group_id TEXT NOT NULL,
    name TEXT NOT NULL,
    abbreviation TEXT,
    is_supplemental BOOLEAN NOT NULL DEFAULT FALSE,
    published_on_source TEXT,
    modified_on_source TEXT,
    source_payload JSONB NOT NULL DEFAULT '{}'::JSONB,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT provider_product_groups_pk
        PRIMARY KEY (
            game_id, provider_id, external_category_id, external_group_id
        ),
    CONSTRAINT provider_product_groups_provider_fk
        FOREIGN KEY (game_id, provider_id)
        REFERENCES catalog.game_providers(game_id, provider_id)
        ON DELETE RESTRICT,
    CONSTRAINT provider_product_groups_category_fk
        FOREIGN KEY (game_id, provider_id, external_category_id)
        REFERENCES catalog.game_providers(
            game_id, provider_id, provider_category_id
        )
        ON DELETE RESTRICT,
    CONSTRAINT provider_product_groups_category_nonempty
        CHECK (btrim(external_category_id) <> ''),
    CONSTRAINT provider_product_groups_id_nonempty
        CHECK (btrim(external_group_id) <> ''),
    CONSTRAINT provider_product_groups_name_nonempty
        CHECK (btrim(name) <> ''),
    CONSTRAINT provider_product_groups_payload_object
        CHECK (jsonb_typeof(source_payload) = 'object')
);

CREATE INDEX catalog_provider_product_groups_name_idx
    ON catalog.provider_product_groups (
        game_id, provider_id, lower(btrim(name))
    );

CREATE TABLE catalog.provider_products (
    game_id TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    external_product_id TEXT NOT NULL,
    external_category_id TEXT NOT NULL,
    external_group_id TEXT NOT NULL,
    name TEXT NOT NULL,
    clean_name TEXT,
    product_url TEXT,
    image_url TEXT,
    image_count INTEGER,
    modified_on_source TEXT,
    presale_info JSONB,
    extended_data JSONB NOT NULL DEFAULT '[]'::JSONB,
    source_payload JSONB NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT provider_products_pk
        PRIMARY KEY (game_id, provider_id, external_product_id),
    CONSTRAINT provider_products_provider_fk
        FOREIGN KEY (game_id, provider_id)
        REFERENCES catalog.game_providers(game_id, provider_id)
        ON DELETE RESTRICT,
    CONSTRAINT provider_products_group_fk
        FOREIGN KEY (
            game_id, provider_id, external_category_id, external_group_id
        )
        REFERENCES catalog.provider_product_groups (
            game_id, provider_id, external_category_id, external_group_id
        )
        ON DELETE RESTRICT,
    CONSTRAINT provider_products_id_nonempty
        CHECK (btrim(external_product_id) <> ''),
    CONSTRAINT provider_products_name_nonempty
        CHECK (btrim(name) <> ''),
    CONSTRAINT provider_products_image_count_nonnegative
        CHECK (image_count IS NULL OR image_count >= 0),
    CONSTRAINT provider_products_extended_data_array
        CHECK (jsonb_typeof(extended_data) = 'array'),
    CONSTRAINT provider_products_payload_object
        CHECK (jsonb_typeof(source_payload) = 'object')
);

CREATE INDEX catalog_provider_products_group_idx
    ON catalog.provider_products (
        game_id, provider_id, external_category_id, external_group_id
    );

CREATE INDEX catalog_provider_products_name_idx
    ON catalog.provider_products (
        game_id, provider_id, lower(btrim(clean_name))
    )
    WHERE clean_name IS NOT NULL;

-- Each source subtype is a separate observation. source_updated remains TEXT
-- to preserve the provider's exact representation and legacy deduplication
-- semantics. Price rows intentionally do not require a product metadata row:
-- the provider's price feed can outlive or precede its product catalog.

CREATE TABLE catalog.provider_price_observations (
    game_id TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    external_product_id TEXT NOT NULL,
    subtype_name TEXT NOT NULL,
    source_updated TEXT NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    market_price NUMERIC,
    low_price NUMERIC,
    mid_price NUMERIC,
    high_price NUMERIC,
    direct_low_price NUMERIC,
    source_payload JSONB NOT NULL,
    CONSTRAINT provider_price_observations_pk
        PRIMARY KEY (
            game_id, provider_id, external_product_id,
            subtype_name, source_updated
        ),
    CONSTRAINT provider_price_observations_provider_fk
        FOREIGN KEY (game_id, provider_id)
        REFERENCES catalog.game_providers(game_id, provider_id)
        ON DELETE RESTRICT,
    CONSTRAINT provider_price_observations_product_nonempty
        CHECK (btrim(external_product_id) <> ''),
    CONSTRAINT provider_price_observations_subtype_nonempty
        CHECK (btrim(subtype_name) <> ''),
    CONSTRAINT provider_price_observations_source_nonempty
        CHECK (btrim(source_updated) <> ''),
    CONSTRAINT provider_price_observations_payload_object
        CHECK (jsonb_typeof(source_payload) = 'object')
);

CREATE INDEX catalog_provider_price_observations_lookup_idx
    ON catalog.provider_price_observations (
        game_id, provider_id, external_product_id,
        subtype_name, captured_at DESC
    );

COMMENT ON TABLE catalog.provider_product_groups IS
    'Provider-scoped product groups. Source publication and modification timestamps remain TEXT because timezone semantics may be unspecified.';

COMMENT ON TABLE catalog.provider_products IS
    'Provider-scoped products with source-preserving metadata. extended_data retains game-specific properties without imposing a shared game schema.';

COMMENT ON TABLE catalog.provider_price_observations IS
    'Provider price observations by exact source subtype and source build. All five TCGCSV price fields are independently nullable.';

COMMENT ON COLUMN catalog.provider_price_observations.source_updated IS
    'Exact provider source-update text; do not normalize before deduplication.';

COMMENT ON COLUMN catalog.provider_price_observations.subtype_name IS
    'Exact provider subtype, such as Normal or Foil. Do not collapse subtypes into MTG finish labels.';
