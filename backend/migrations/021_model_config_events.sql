ALTER TABLE agent_capability_events DROP CONSTRAINT agent_capability_events_kind_check;
ALTER TABLE agent_capability_events ADD CONSTRAINT agent_capability_events_kind_check
    CHECK(kind IN ('tool','skill','model'));
INSERT INTO schema_migrations(version) VALUES(21);
