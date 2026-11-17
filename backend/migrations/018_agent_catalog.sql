CREATE TABLE agent_model_profiles (
    profile_id text PRIMARY KEY CHECK(length(profile_id) BETWEEN 1 AND 64),
    name text NOT NULL,
    version integer NOT NULL CHECK(version > 0),
    model_id text NOT NULL,
    base_url text NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE agent_skills (
    skill_id uuid PRIMARY KEY,
    name text NOT NULL CHECK(length(btrim(name)) BETWEEN 1 AND 80),
    enabled boolean NOT NULL DEFAULT true,
    revision integer NOT NULL DEFAULT 1 CHECK(revision > 0),
    current_version integer NOT NULL CHECK(current_version > 0),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE UNIQUE INDEX agent_skills_name ON agent_skills(lower(btrim(name)));

CREATE TABLE agent_skill_versions (
    skill_id uuid NOT NULL REFERENCES agent_skills(skill_id),
    version integer NOT NULL CHECK(version > 0),
    config jsonb NOT NULL,
    published_by text NOT NULL REFERENCES users(user_id),
    published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(skill_id, version)
);

CREATE TABLE agent_tool_states (
    tool_name text PRIMARY KEY,
    enabled boolean NOT NULL,
    revision integer NOT NULL CHECK(revision > 0),
    updated_by text NOT NULL REFERENCES users(user_id),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
INSERT INTO schema_migrations(version) VALUES(18);
