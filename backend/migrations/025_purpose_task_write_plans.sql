ALTER TABLE write_plans
    ADD COLUMN task_id uuid REFERENCES tasks,
    ADD COLUMN task_generation bigint,
    ADD COLUMN task_period text,
    ADD CONSTRAINT write_plan_task_scope CHECK (
        (task_id IS NULL AND task_generation IS NULL AND task_period IS NULL) OR
        (task_id IS NOT NULL AND task_generation IS NOT NULL AND task_period IS NOT NULL)
    );

CREATE INDEX write_plans_task_generation ON write_plans(task_id, task_generation)
    WHERE task_id IS NOT NULL;

INSERT INTO schema_migrations(version) VALUES(25);
