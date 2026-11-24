-- Durable provider-scoped synchronization metadata for catalog workers.
CREATE TABLE catalog.provider_sync_state (
    game_id TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    last_attempt_at TIMESTAMPTZ,
    last_success_at TIMESTAMPTZ,
    source_revision JSONB NOT NULL DEFAULT '{}'::jsonb,
    import_counts JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_error TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT provider_sync_state_pk PRIMARY KEY (game_id, provider_id),
    CONSTRAINT provider_sync_state_provider_fk
        FOREIGN KEY (game_id, provider_id)
        REFERENCES catalog.game_providers(game_id, provider_id)
        ON DELETE RESTRICT,
    CONSTRAINT provider_sync_state_revision_object CHECK (jsonb_typeof(source_revision) = 'object'),
    CONSTRAINT provider_sync_state_counts_object CHECK (jsonb_typeof(import_counts) = 'object')
);

COMMENT ON TABLE catalog.provider_sync_state IS
    'Last attempt, success, source revision, import counts, and error for provider sync workers.';
