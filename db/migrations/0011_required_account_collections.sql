-- Every login account owns exactly one collection through users.person_id.
-- Preserve all existing account-to-person links; only unlinked accounts receive
-- newly-created, dedicated collections. No existing collection is reassigned.

CREATE OR REPLACE FUNCTION app.ensure_user_collection()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    base_name TEXT;
    candidate_name TEXT;
    suffix INTEGER := 1;
    created_person_id BIGINT;
BEGIN
    IF NEW.person_id IS NULL THEN
        base_name := left(btrim(NEW.username), 72) || ' Collection';
        candidate_name := base_name;

        LOOP
            created_person_id := NULL;
            INSERT INTO app.people (name, active)
            VALUES (candidate_name, TRUE)
            ON CONFLICT DO NOTHING
            RETURNING id INTO created_person_id;

            EXIT WHEN created_person_id IS NOT NULL;

            suffix := suffix + 1;
            IF suffix > 10000 THEN
                RAISE EXCEPTION 'Could not create a unique collection for account %', NEW.username;
            END IF;
            candidate_name :=
                left(base_name, 80 - length(' (' || suffix::TEXT || ')'))
                || ' (' || suffix::TEXT || ')';
        END LOOP;

        NEW.person_id := created_person_id;
    END IF;

    RETURN NEW;
END;
$$;

-- This trigger also protects accounts created by importers or other code paths
-- that do not go through the Administration form.
CREATE TRIGGER app_users_ensure_collection
BEFORE INSERT OR UPDATE OF person_id ON app.users
FOR EACH ROW
EXECUTE FUNCTION app.ensure_user_collection();

-- Backfill through the same trigger. Existing non-null assignments are untouched.
UPDATE app.users
SET person_id = NULL
WHERE person_id IS NULL;

ALTER TABLE app.users
    ALTER COLUMN person_id SET NOT NULL;

-- A collection linked to an account cannot be deleted out from underneath it.
ALTER TABLE app.users
    DROP CONSTRAINT users_person_fk;

ALTER TABLE app.users
    ADD CONSTRAINT users_person_fk
    FOREIGN KEY (person_id)
    REFERENCES app.people(id)
    ON DELETE RESTRICT;

-- The previous partial unique index becomes a full one-to-one constraint now
-- that person_id is mandatory for every account.
DROP INDEX app.app_users_person_id_unique;

CREATE UNIQUE INDEX app_users_person_id_unique
    ON app.users (person_id);

CREATE OR REPLACE FUNCTION app.prevent_deleting_account_collection()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM app.users WHERE person_id = OLD.id
    ) THEN
        RAISE EXCEPTION 'Collection % is assigned to an account and cannot be deleted', OLD.name
            USING ERRCODE = '23503';
    END IF;
    RETURN OLD;
END;
$$;

CREATE TRIGGER app_people_prevent_account_collection_delete
BEFORE DELETE ON app.people
FOR EACH ROW
EXECUTE FUNCTION app.prevent_deleting_account_collection();

COMMENT ON COLUMN app.users.person_id IS
    'Exactly one collection owned by this login account. Unique and NOT NULL; account managers do not own managed users’ collections.';
