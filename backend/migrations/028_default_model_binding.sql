CREATE TABLE default_model_versions (
    version bigint PRIMARY KEY CHECK (version > 0),
    source text NOT NULL CHECK (source IN ('env', 'page')),
    model_id text NOT NULL,
    config_fingerprint text NOT NULL,
    activated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

ALTER TABLE turns ADD COLUMN default_model_version bigint REFERENCES default_model_versions(version);

-- A legacy queued request never recorded which default connection accepted it.
-- Leave it unbound so a worker can report that uncertainty without dispatch.
INSERT INTO schema_migrations(version) VALUES(28);
