CREATE TABLE workbook_snapshots (
    version_id uuid PRIMARY KEY REFERENCES knowledge_versions ON DELETE CASCADE,
    snapshot jsonb NOT NULL
);

CREATE TABLE workbook_results (
    result_id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES runs ON DELETE CASCADE,
    version_id uuid NOT NULL REFERENCES workbook_snapshots ON DELETE CASCADE,
    query jsonb NOT NULL,
    result jsonb NOT NULL
);
CREATE INDEX workbook_results_run ON workbook_results(run_id);

INSERT INTO schema_migrations(version) VALUES(24);
