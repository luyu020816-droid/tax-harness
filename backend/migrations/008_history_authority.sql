CREATE TABLE history_jobs (
    source_id uuid PRIMARY KEY REFERENCES sources ON DELETE CASCADE,
    source_revision bigint NOT NULL,
    state text NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','running','done','blocked','cancelled','skipped')),
    profile_id text,
    attempts integer NOT NULL DEFAULT 0,
    fencing_token bigint NOT NULL DEFAULT 0,
    lease_until timestamptz,
    error jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE history_passages (
    passage_id uuid PRIMARY KEY,
    source_id uuid NOT NULL REFERENCES sources ON DELETE CASCADE,
    source_revision bigint NOT NULL,
    profile_id text NOT NULL REFERENCES retrieval_profiles,
    chunker text NOT NULL,
    ordinal integer NOT NULL,
    char_start integer NOT NULL,
    char_end integer NOT NULL,
    excerpt text NOT NULL,
    excerpt_hash text NOT NULL,
    lexical_index tsvector NOT NULL,
    CHECK(char_start>=0 AND char_end>char_start),
    UNIQUE(source_id,source_revision,profile_id,ordinal)
);
CREATE INDEX history_passages_lexical ON history_passages USING gin(lexical_index);
CREATE INDEX history_passages_source ON history_passages(source_id,source_revision,profile_id);
CREATE TABLE history_queries (
    query_id uuid PRIMARY KEY,
    user_id text NOT NULL REFERENCES users,
    company_id text REFERENCES companies,
    query_plan jsonb NOT NULL,
    scope_snapshot jsonb NOT NULL,
    status text NOT NULL,
    retrieval_mode text NOT NULL,
    profile_id text,
    coverage jsonb NOT NULL,
    candidates jsonb NOT NULL,
    timings jsonb NOT NULL,
    pagination jsonb NOT NULL,
    degraded_reason text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE history_evidence (
    citation_id uuid PRIMARY KEY,
    query_id uuid NOT NULL REFERENCES history_queries ON DELETE CASCADE,
    source_id uuid NOT NULL REFERENCES sources,
    source_revision bigint NOT NULL,
    char_start integer NOT NULL,
    char_end integer NOT NULL,
    excerpt text NOT NULL,
    excerpt_hash text NOT NULL,
    rank integer NOT NULL,
    UNIQUE(query_id,rank)
);
INSERT INTO history_jobs(source_id,source_revision)
SELECT source_id,revision FROM sources WHERE recall_state='active';
INSERT INTO schema_migrations(version) VALUES(8);
