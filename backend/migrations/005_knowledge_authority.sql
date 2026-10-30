ALTER TABLE user_company_grants ADD COLUMN can_upload_knowledge boolean NOT NULL DEFAULT false;
ALTER TABLE user_company_grants ADD CHECK(NOT can_upload_knowledge OR can_read);
UPDATE user_company_grants SET can_upload_knowledge=true WHERE user_id='U001' AND can_read;
ALTER TABLE company_heads ADD COLUMN knowledge_epoch bigint NOT NULL DEFAULT 1;
ALTER TABLE turns ADD COLUMN knowledge_upload_ids uuid[] NOT NULL DEFAULT '{}';
ALTER TABLE rule_versions ADD COLUMN execution_profile text NOT NULL DEFAULT 'tax-baseline-v1';

CREATE TABLE knowledge_files (
    file_id uuid PRIMARY KEY,
    owner_id text NOT NULL REFERENCES users ON DELETE CASCADE,
    company_id text NOT NULL REFERENCES companies ON DELETE CASCADE,
    original_name text NOT NULL,
    media_type text NOT NULL,
    file_bytes bytea NOT NULL,
    byte_size bigint NOT NULL CHECK(byte_size>0 AND byte_size<=20971520),
    sha256 text NOT NULL CHECK(length(sha256)=64),
    request_id text NOT NULL,
    request_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(owner_id,request_id), UNIQUE(file_id,owner_id,company_id),
    CHECK(octet_length(file_bytes)=byte_size)
);
CREATE TABLE knowledge_documents (
    document_id uuid PRIMARY KEY,
    owner_id text NOT NULL REFERENCES users ON DELETE CASCADE,
    company_id text NOT NULL REFERENCES companies ON DELETE CASCADE,
    scope text NOT NULL CHECK(scope IN ('user_private','company')),
    title text NOT NULL,
    revision bigint NOT NULL DEFAULT 1,
    status text NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled')),
    active_version_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(document_id,owner_id,company_id)
);
CREATE TABLE retrieval_profiles (
    profile_id text PRIMARY KEY,
    manifest jsonb NOT NULL,
    dimension integer NOT NULL CHECK(dimension=1024),
    status text NOT NULL CHECK(status IN ('ready','disabled')),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE knowledge_versions (
    version_id uuid PRIMARY KEY,
    document_id uuid NOT NULL,
    owner_id text NOT NULL,
    company_id text NOT NULL,
    file_id uuid NOT NULL UNIQUE,
    edition integer NOT NULL CHECK(edition>0),
    effective_from date NOT NULL DEFAULT '0001-01-01',
    effective_to date CHECK(effective_to>effective_from),
    expected_document_revision bigint NOT NULL,
    state text NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','parsing','chunking','embedding','validating','ready','failed','blocked','cancelled')),
    parser_version text,
    chunker_version text,
    profile_id text REFERENCES retrieval_profiles,
    content_hash text,
    block_count integer NOT NULL DEFAULT 0,
    chunk_count integer NOT NULL DEFAULT 0,
    error jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(document_id,edition), UNIQUE(document_id,version_id),
    FOREIGN KEY(document_id,owner_id,company_id) REFERENCES knowledge_documents(document_id,owner_id,company_id) ON DELETE CASCADE,
    FOREIGN KEY(file_id,owner_id,company_id) REFERENCES knowledge_files(file_id,owner_id,company_id) ON DELETE CASCADE
);
ALTER TABLE knowledge_documents ADD CONSTRAINT active_version_belongs_to_document
    FOREIGN KEY(document_id,active_version_id) REFERENCES knowledge_versions(document_id,version_id) DEFERRABLE INITIALLY DEFERRED;
CREATE TABLE knowledge_blocks (
    block_id uuid PRIMARY KEY,
    version_id uuid NOT NULL REFERENCES knowledge_versions ON DELETE CASCADE,
    ordinal integer NOT NULL CHECK(ordinal>=0),
    kind text NOT NULL,
    heading_path text NOT NULL,
    content text NOT NULL CHECK(content<>''),
    content_hash text NOT NULL,
    locator jsonb NOT NULL,
    UNIQUE(version_id,ordinal)
);
CREATE TABLE knowledge_chunks (
    chunk_id uuid PRIMARY KEY,
    version_id uuid NOT NULL REFERENCES knowledge_versions ON DELETE CASCADE,
    ordinal integer NOT NULL,
    block_ordinals integer[] NOT NULL CHECK(cardinality(block_ordinals)>0),
    content text NOT NULL,
    retrieval_text text NOT NULL,
    content_hash text NOT NULL,
    token_count integer NOT NULL CHECK(token_count BETWEEN 1 AND 1200),
    lexical_index tsvector NOT NULL,
    locator jsonb NOT NULL,
    UNIQUE(version_id,ordinal)
);
CREATE INDEX knowledge_chunks_lexical ON knowledge_chunks USING gin(lexical_index);
CREATE TABLE retrieval_jobs (
    job_id uuid PRIMARY KEY,
    user_id text NOT NULL REFERENCES users ON DELETE CASCADE,
    version_id uuid NOT NULL UNIQUE REFERENCES knowledge_versions ON DELETE CASCADE,
    state text NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','running','done','failed','blocked','cancelled')),
    stage text NOT NULL DEFAULT 'queued',
    attempts integer NOT NULL DEFAULT 0,
    lease_until timestamptz,
    fencing_token bigint NOT NULL DEFAULT 0,
    progress jsonb NOT NULL DEFAULT '{}',
    error jsonb,
    retry_request_id text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX retrieval_jobs_ready ON retrieval_jobs(state,lease_until);
CREATE TABLE retrieval_queries (
    query_id uuid PRIMARY KEY,
    user_id text NOT NULL REFERENCES users ON DELETE CASCADE,
    session_id uuid REFERENCES sessions ON DELETE CASCADE,
    turn_id uuid REFERENCES turns ON DELETE CASCADE,
    company_id text NOT NULL REFERENCES companies ON DELETE CASCADE,
    corpus text NOT NULL DEFAULT 'knowledge',
    raw_query text NOT NULL,
    profile_id text REFERENCES retrieval_profiles,
    status text NOT NULL,
    retrieval_mode text NOT NULL,
    scope_snapshot jsonb NOT NULL,
    coverage jsonb NOT NULL,
    candidates jsonb NOT NULL DEFAULT '[]',
    timings jsonb NOT NULL DEFAULT '{}',
    degraded_reason text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE retrieval_evidence (
    citation_id uuid PRIMARY KEY,
    query_id uuid NOT NULL REFERENCES retrieval_queries ON DELETE CASCADE,
    document_id uuid NOT NULL REFERENCES knowledge_documents ON DELETE CASCADE,
    document_revision bigint NOT NULL,
    version_id uuid NOT NULL REFERENCES knowledge_versions ON DELETE CASCADE,
    chunk_id uuid NOT NULL REFERENCES knowledge_chunks ON DELETE CASCADE,
    excerpt text NOT NULL,
    excerpt_hash text NOT NULL,
    locator jsonb NOT NULL,
    rank integer NOT NULL,
    UNIQUE(query_id,chunk_id)
);
CREATE TABLE rule_knowledge_refs (
    rule_version_id uuid PRIMARY KEY REFERENCES rule_versions ON DELETE CASCADE,
    knowledge_version_id uuid NOT NULL REFERENCES knowledge_versions,
    block_id uuid NOT NULL REFERENCES knowledge_blocks,
    locator jsonb NOT NULL,
    excerpt_hash text NOT NULL
);
INSERT INTO schema_migrations(version) VALUES(5);
