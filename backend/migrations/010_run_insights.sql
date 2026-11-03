CREATE TABLE trace_spans (
    span_id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES runs ON DELETE CASCADE,
    parent_span_id uuid,
    logical_key text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('context','model','tool','retrieval','write_verify','host')),
    name text NOT NULL,
    status text NOT NULL CHECK (status IN ('running','succeeded','failed','cancelled','interrupted','not_dispatched')),
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    ended_at timestamptz,
    duration_ms double precision CHECK (duration_ms >= 0),
    measurement_kind text NOT NULL DEFAULT 'measured' CHECK (measurement_kind IN ('measured','event_derived','estimated','unknown')),
    context_id uuid REFERENCES context_snapshots,
    tool_call_id text,
    error_code text,
    attributes jsonb NOT NULL DEFAULT '{}',
    UNIQUE (run_id, logical_key),
    UNIQUE (run_id, span_id),
    FOREIGN KEY (run_id, parent_span_id) REFERENCES trace_spans(run_id, span_id),
    CHECK (ended_at IS NULL OR ended_at >= started_at),
    CHECK (octet_length(attributes::text) <= 65536)
);
CREATE INDEX trace_spans_run_parent ON trace_spans(run_id,parent_span_id,started_at);
CREATE UNIQUE INDEX trace_spans_model_context ON trace_spans(context_id) WHERE kind='model' AND context_id IS NOT NULL;
ALTER TABLE runs ADD COLUMN insight_snapshot jsonb;
CREATE INDEX turns_insight_history ON turns(user_id,created_at DESC,turn_id DESC);
CREATE INDEX sources_insight_turn ON sources(turn_id);
CREATE VIEW insight_attempts AS
SELECT s.run_id,s.context_id,s.span_id,s.attributes->>'model' AS model,
       s.status,s.duration_ms,s.attributes->'usage' AS usage,
       (s.attributes->>'request_dispatched')::boolean AS dispatched,
       s.measurement_kind,s.started_at
FROM trace_spans s WHERE s.kind='model'
UNION ALL
SELECT c.run_id,c.context_id,NULL::uuid,c.payload->>'model',
       CASE WHEN c.duration_ms IS NOT NULL OR c.usage IS NOT NULL OR EXISTS(
           SELECT 1 FROM run_events e WHERE e.input_ref=c.context_id AND e.kind IN ('model_finished','compaction_model_finished')
       ) THEN 'succeeded' ELSE 'interrupted' END,
       c.duration_ms,c.usage,
       CASE WHEN c.duration_ms IS NOT NULL OR c.usage IS NOT NULL OR EXISTS(
           SELECT 1 FROM run_events e WHERE e.input_ref=c.context_id AND e.kind IN ('model_finished','compaction_model_finished')
       ) THEN true ELSE NULL::boolean END,
       'event_derived',c.created_at
FROM context_snapshots c
WHERE coalesce(c.manifest->>'kind','')<>'control_execution'
AND NOT EXISTS(SELECT 1 FROM trace_spans s WHERE s.context_id=c.context_id AND s.kind='model');
CREATE TABLE insight_evaluations (
    evaluation_id uuid PRIMARY KEY,
    owner_id text NOT NULL REFERENCES users,
    company_ids jsonb NOT NULL DEFAULT '[]',
    report_hash text NOT NULL,
    title text NOT NULL,
    report jsonb NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(owner_id,report_hash)
);
INSERT INTO schema_migrations(version) VALUES (10);
