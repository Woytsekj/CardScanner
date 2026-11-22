-- Remove the experimental many-collections-per-account mapping.
-- Each login account has exactly one collection through app.users.person_id.
-- The unique app_users_person_id_unique index from migration 0004 ensures
-- that each collection is linked to at most one account.
DROP TABLE IF EXISTS app.user_people;

COMMENT ON COLUMN app.users.person_id IS
    'The sole collection owned by this login account. A collection can be assigned to at most one account.';
