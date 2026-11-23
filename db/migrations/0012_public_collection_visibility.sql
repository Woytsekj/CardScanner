-- Opt-in public visibility belongs to a collection, not to the account manager.
-- Public collections expose inventory cards in the viewer as read-only data.
ALTER TABLE app.people
    ADD COLUMN IF NOT EXISTS is_public BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS app_people_public_active_idx
    ON app.people (lower(btrim(name)))
    WHERE active IS TRUE AND is_public IS TRUE;

COMMENT ON COLUMN app.people.is_public IS
    'Owner-controlled opt-in visibility. When true, the collection inventory can be included in other signed-in users public-card viewer; it does not grant edit permissions.';
