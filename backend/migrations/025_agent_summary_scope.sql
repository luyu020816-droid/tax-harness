ALTER TABLE summaries ADD COLUMN agent_scope text NOT NULL DEFAULT '';
CREATE INDEX summaries_agent_scope ON summaries(user_id,session_id,scope,agent_scope,memory_epoch,generation DESC);
INSERT INTO schema_migrations(version) VALUES(25);
