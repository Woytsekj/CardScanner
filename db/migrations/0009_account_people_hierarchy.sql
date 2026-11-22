-- Multi-person account ownership and manager hierarchy.
-- Keep users.person_id as the default collection for backwards compatibility.
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

CREATE TABLE app.user_people (
    username TEXT NOT NULL,
    person_id BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT user_people_pk PRIMARY KEY (username, person_id),
    CONSTRAINT user_people_user_fk
        FOREIGN KEY (username) REFERENCES app.users(username) ON DELETE CASCADE,
    CONSTRAINT user_people_person_fk
        FOREIGN KEY (person_id) REFERENCES app.people(id) ON DELETE CASCADE
);

-- One person/collection is assigned to one account; each account can own many.
CREATE UNIQUE INDEX app_user_people_person_unique
    ON app.user_people (person_id);
CREATE INDEX app_user_people_username_idx
    ON app.user_people (lower(username));

INSERT INTO app.user_people (username, person_id)
SELECT username, person_id
FROM app.users
WHERE person_id IS NOT NULL
ON CONFLICT DO NOTHING;

COMMENT ON TABLE app.user_people IS
    'People/collections associated with a web account. users.person_id remains the default collection.';
COMMENT ON COLUMN app.users.manager_username IS
    'Admin account responsible for a normal user. Superadmins retain global oversight.';
