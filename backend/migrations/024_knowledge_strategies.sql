CREATE TABLE knowledge_strategies (
    company_id text PRIMARY KEY REFERENCES companies ON DELETE CASCADE,
    revision integer NOT NULL CHECK (revision > 0)
);
CREATE TABLE knowledge_strategy_versions (
    company_id text NOT NULL REFERENCES knowledge_strategies ON DELETE CASCADE,
    version integer NOT NULL CHECK (version > 0),
    config jsonb NOT NULL,
    actor_id text NOT NULL REFERENCES users,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(company_id,version)
);
INSERT INTO schema_migrations(version) VALUES(24);
