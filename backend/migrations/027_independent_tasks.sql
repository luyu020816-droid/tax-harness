-- A slot remains the focus and generation counter for a user and company.
-- The tasks themselves now carry independent active lifecycles.
CREATE UNIQUE INDEX tasks_unfinished_scope ON tasks(user_id,company_id,intent,period)
    NULLS NOT DISTINCT WHERE status IN ('active','waiting_user');
DROP INDEX tasks_one_unfinished;
INSERT INTO schema_migrations(version) VALUES(27);
