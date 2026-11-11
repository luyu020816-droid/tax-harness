-- Apply after stopping old workers so no process sees a partially upgraded protocol.
ALTER TABLE turns DROP CONSTRAINT IF EXISTS turns_request_kind_check;
ALTER TABLE turns ADD CONSTRAINT turns_request_kind_check
    CHECK(request_kind IN ('chat','write_confirmation','compact'));

CREATE TABLE compaction_requests (
    turn_id uuid PRIMARY KEY REFERENCES turns,
    user_id text NOT NULL REFERENCES users,
    session_id uuid NOT NULL REFERENCES sessions,
    selected_company_id text,
    model_fingerprint text NOT NULL,
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX compaction_requests_session ON compaction_requests(user_id,session_id,created_at DESC);
INSERT INTO schema_migrations(version) VALUES(16);
