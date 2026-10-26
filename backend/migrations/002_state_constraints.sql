ALTER TABLE turns ADD CONSTRAINT turns_state CHECK(status IN ('queued','running','waiting_user','completed','cancelled','interrupted','failed','resolved','deleted'));
ALTER TABLE work_items ADD CONSTRAINT work_state CHECK(state IN ('ready','running','done','failed','cancelled','invalidated'));
ALTER TABLE operations ADD CONSTRAINT operations_state CHECK(state IN ('prepared','committed','no_change','rejected'));
ALTER TABLE operations ADD CONSTRAINT verification_state CHECK(verification_state IN ('pending','verified','changed_since_commit'));
ALTER TABLE sources ADD CONSTRAINT recall_state CHECK(recall_state IN ('active','suppressed','deleted'));
ALTER TABLE source_assessments ADD CONSTRAINT assessment_state CHECK(state IN ('pending','accepted','no_change','failed','invalidated'));
ALTER TABLE tasks ADD CONSTRAINT task_period CHECK(period IS NULL OR period ~ '^[0-9]{4}-(0[1-9]|1[0-2])$');
INSERT INTO schema_migrations(version) VALUES(2);
