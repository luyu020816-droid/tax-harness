CREATE TABLE agent_turn_bindings (
    turn_id uuid PRIMARY KEY REFERENCES turns(turn_id) ON DELETE CASCADE,
    agent_id uuid NOT NULL,
    agent_version integer NOT NULL,
    snapshot jsonb NOT NULL,
    bound_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY(agent_id,agent_version) REFERENCES agent_profile_versions(agent_id,version)
);
INSERT INTO schema_migrations(version) VALUES(20);
