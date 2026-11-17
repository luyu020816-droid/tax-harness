CREATE TABLE agent_capability_events (
    event_id uuid PRIMARY KEY,
    actor_id text NOT NULL REFERENCES users(user_id),
    kind text NOT NULL CHECK(kind IN ('tool','skill')),
    capability_id text NOT NULL,
    action text NOT NULL CHECK(action IN ('publish','enable','disable')),
    revision integer NOT NULL CHECK(revision > 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
INSERT INTO schema_migrations(version) VALUES(19);
