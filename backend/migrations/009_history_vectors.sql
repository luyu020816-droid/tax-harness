CREATE TABLE history_embeddings (
    passage_id uuid PRIMARY KEY REFERENCES history_passages ON DELETE CASCADE,
    embedding vector(1024) NOT NULL
);
INSERT INTO schema_migrations(version) VALUES(9);
