CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE retrieval_embeddings (
    profile_id text NOT NULL REFERENCES retrieval_profiles ON DELETE CASCADE,
    target_kind text NOT NULL DEFAULT 'knowledge' CHECK(target_kind='knowledge'),
    target_id uuid NOT NULL REFERENCES knowledge_chunks ON DELETE CASCADE,
    target_revision bigint NOT NULL DEFAULT 1,
    input_hash text NOT NULL,
    embedding vector(1024) NOT NULL,
    PRIMARY KEY(profile_id,target_kind,target_id,target_revision)
);
INSERT INTO schema_migrations(version) VALUES(6);
