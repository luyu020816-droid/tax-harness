ALTER TABLE rule_knowledge_refs ADD COLUMN company_id text REFERENCES companies;
UPDATE rule_knowledge_refs k SET company_id=v.company_id FROM knowledge_versions v WHERE v.version_id=k.knowledge_version_id;
ALTER TABLE rule_knowledge_refs ALTER COLUMN company_id SET NOT NULL;
ALTER TABLE rule_knowledge_refs DROP CONSTRAINT rule_knowledge_refs_pkey;
ALTER TABLE rule_knowledge_refs ADD PRIMARY KEY(rule_version_id,company_id);
ALTER TABLE rule_knowledge_refs ADD FOREIGN KEY(rule_version_id,company_id) REFERENCES rule_scopes(rule_version_id,company_id);
ALTER TABLE knowledge_blocks ADD UNIQUE(block_id,version_id);
ALTER TABLE rule_knowledge_refs ADD FOREIGN KEY(block_id,knowledge_version_id) REFERENCES knowledge_blocks(block_id,version_id);
CREATE TABLE knowledge_maintenance_ops (
    request_id text PRIMARY KEY,
    request_hash text NOT NULL,
    actor text NOT NULL DEFAULT 'local_maintainer',
    action text NOT NULL,
    result jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO schema_migrations(version) VALUES(7);
