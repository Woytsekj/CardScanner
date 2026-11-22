-- Account manager hierarchy.
-- users.person_id remains the single account-to-collection relationship.
ALTER TABLE app.users
    ADD COLUMN manager_username TEXT;

ALTER TABLE app.users
    ADD CONSTRAINT users_manager_fk
    FOREIGN KEY (manager_username)
    REFERENCES app.users(username)
    ON DELETE SET NULL;

CREATE INDEX app_users_manager_idx
    ON app.users (lower(manager_username))
    WHERE manager_username IS NOT NULL;

COMMENT ON COLUMN app.users.manager_username IS
    'Admin account responsible for a normal user. Superadmins retain global oversight.';

-- Existing non-superadmin accounts inherit the elected superadmin as manager
-- until a more specific manager assignment is chosen in Administration.
UPDATE app.users
SET manager_username = (
    SELECT username FROM app.users
    WHERE is_superadmin IS TRUE AND active IS TRUE
    ORDER BY created_at, lower(username) COLLATE "C"
    LIMIT 1
)
WHERE is_superadmin IS FALSE AND manager_username IS NULL;
