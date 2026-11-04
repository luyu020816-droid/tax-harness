-- Disposable lexical index, separate from manually submitted knowledge.
CREATE TABLE conversation_terms (
    source_id uuid PRIMARY KEY REFERENCES sources,
    source_hash text NOT NULL,
    lexical_version text NOT NULL,
    lexical_index tsvector NOT NULL
);
CREATE INDEX conversation_terms_lexical ON conversation_terms USING gin(lexical_index);
INSERT INTO schema_migrations(version) VALUES(14);
