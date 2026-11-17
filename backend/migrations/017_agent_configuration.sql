-- Configuration records do not change existing session or business authority.
CREATE TABLE agent_config_managers (
    user_id text PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE
);

CREATE TABLE agent_profiles (
    agent_id uuid PRIMARY KEY,
    name text NOT NULL CHECK(length(btrim(name)) BETWEEN 1 AND 80),
    enabled boolean NOT NULL DEFAULT false,
    revision integer NOT NULL DEFAULT 1 CHECK(revision >= 1),
    draft jsonb NOT NULL,
    current_version integer,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE UNIQUE INDEX agent_profiles_name ON agent_profiles(lower(btrim(name)));

CREATE TABLE agent_profile_versions (
    agent_id uuid NOT NULL REFERENCES agent_profiles(agent_id),
    version integer NOT NULL CHECK(version >= 1),
    config jsonb NOT NULL,
    published_by text NOT NULL REFERENCES users(user_id),
    published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(agent_id, version)
);

CREATE TABLE agent_config_events (
    event_id uuid PRIMARY KEY,
    agent_id uuid NOT NULL REFERENCES agent_profiles(agent_id),
    actor_id text NOT NULL REFERENCES users(user_id),
    action text NOT NULL CHECK(action IN ('create','edit','publish','enable','disable')),
    revision integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

INSERT INTO agent_config_managers(user_id)
SELECT user_id FROM users WHERE user_id='U001' ON CONFLICT DO NOTHING;

INSERT INTO schema_migrations(version) VALUES(17);
