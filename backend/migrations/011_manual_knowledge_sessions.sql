-- Conversation records are not knowledge documents. Preserve legacy evidence,
-- but make pre-isolation summaries and history jobs unavailable online.
ALTER TABLE sessions ADD COLUMN pinned_at timestamptz;
ALTER TABLE sessions ADD COLUMN archived_at timestamptz;
ALTER TABLE sessions ADD COLUMN deleted_at timestamptz;
ALTER TABLE sessions ADD COLUMN organization_revision bigint NOT NULL DEFAULT 1;
CREATE INDEX sessions_history ON sessions(user_id,archived_at,updated_at DESC,session_id);
ALTER TABLE summaries ADD COLUMN session_id uuid REFERENCES sessions;
CREATE INDEX summaries_session ON summaries(user_id,session_id,scope,memory_epoch,generation DESC);
ALTER TABLE knowledge_files ADD COLUMN submission_kind text NOT NULL DEFAULT 'file' CHECK(submission_kind IN ('file','manual_text'));
UPDATE summaries SET status='invalidated' WHERE session_id IS NULL AND status='active';
UPDATE history_jobs SET state='cancelled',fencing_token=fencing_token+1,lease_until=NULL
WHERE state IN ('queued','running','blocked');
INSERT INTO schema_migrations(version) VALUES(11);
