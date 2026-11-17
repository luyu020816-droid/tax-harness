-- A fixed-value column allows row locking without permitting grant changes.
ALTER TABLE agent_config_managers ADD COLUMN lock_token boolean NOT NULL DEFAULT true
    CHECK(lock_token);
INSERT INTO schema_migrations(version) VALUES(22);
