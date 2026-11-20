-- Versions 24 and 25 were shipped independently by two branches. Fresh installs
-- run the workbook/plan scripts at those numbers. Older databases may have
-- recorded either pair. Reconcile the actual structures without moving data.
CREATE TABLE IF NOT EXISTS workbook_snapshots (
    version_id uuid PRIMARY KEY REFERENCES knowledge_versions ON DELETE CASCADE,
    snapshot jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS workbook_results (
    result_id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES runs ON DELETE CASCADE,
    version_id uuid NOT NULL REFERENCES workbook_snapshots ON DELETE CASCADE,
    query jsonb NOT NULL,
    result jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS workbook_results_run ON workbook_results(run_id);

ALTER TABLE write_plans
    ADD COLUMN IF NOT EXISTS task_id uuid REFERENCES tasks,
    ADD COLUMN IF NOT EXISTS task_generation bigint,
    ADD COLUMN IF NOT EXISTS task_period text;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.write_plans'::regclass AND conname = 'write_plan_task_scope'
    ) THEN
        ALTER TABLE write_plans ADD CONSTRAINT write_plan_task_scope CHECK (
            (task_id IS NULL AND task_generation IS NULL AND task_period IS NULL) OR
            (task_id IS NOT NULL AND task_generation IS NOT NULL AND task_period IS NOT NULL)
        );
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS write_plans_task_generation ON write_plans(task_id, task_generation)
    WHERE task_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS knowledge_strategies (
    company_id text PRIMARY KEY REFERENCES companies ON DELETE CASCADE,
    revision integer NOT NULL CHECK (revision > 0)
);
CREATE TABLE IF NOT EXISTS knowledge_strategy_versions (
    company_id text NOT NULL REFERENCES knowledge_strategies ON DELETE CASCADE,
    version integer NOT NULL CHECK (version > 0),
    config jsonb NOT NULL,
    actor_id text NOT NULL REFERENCES users,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(company_id,version)
);

ALTER TABLE summaries ADD COLUMN IF NOT EXISTS agent_scope text NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS summaries_agent_scope
    ON summaries(user_id,session_id,scope,agent_scope,memory_epoch,generation DESC);

-- Only give an identity to scripts run by the new initializer. The historical
-- numbers are ambiguous and intentionally retain their original ledger entries.
ALTER TABLE schema_migrations ADD COLUMN script_name text UNIQUE;
INSERT INTO schema_migrations(version) VALUES(26);
