ALTER TABLE user_company_grants ADD COLUMN can_import_invoices boolean NOT NULL DEFAULT false;
ALTER TABLE user_company_grants ADD CONSTRAINT import_requires_read CHECK(NOT can_import_invoices OR can_read);
ALTER TABLE turns ADD COLUMN upload_ids uuid[] NOT NULL DEFAULT '{}';

CREATE TABLE dataset_metadata (
    singleton_id integer PRIMARY KEY CHECK(singleton_id=1),
    instance_id uuid NOT NULL DEFAULT gen_random_uuid(),
    profile text NOT NULL DEFAULT 'competition' CHECK(profile IN ('competition','extended')),
    baseline_id text NOT NULL DEFAULT 'competition-v1.1.0',
    created_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO dataset_metadata(singleton_id) VALUES(1);
CREATE TABLE file_uploads (
    upload_id uuid PRIMARY KEY,
    source_id uuid NOT NULL REFERENCES sources,
    user_id text NOT NULL REFERENCES users,
    company_id text NOT NULL REFERENCES companies,
    session_id uuid NOT NULL REFERENCES sessions,
    client_request_id text NOT NULL,
    request_hash text NOT NULL,
    original_name text NOT NULL,
    file_sha256 text NOT NULL,
    byte_size integer NOT NULL CHECK(byte_size BETWEEN 1 AND 5242880),
    content bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK(octet_length(content)=byte_size),
    UNIQUE(user_id,client_request_id)
);
CREATE INDEX uploads_owner ON file_uploads(user_id,created_at);
CREATE TABLE invoice_import_batches (
    batch_id uuid PRIMARY KEY,
    upload_id uuid NOT NULL REFERENCES file_uploads,
    user_id text NOT NULL REFERENCES users,
    company_id text NOT NULL REFERENCES companies,
    revision bigint NOT NULL DEFAULT 1,
    status text NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','validation_failed','preview_ready','committed','no_change','cancelled','expired')),
    policy text NOT NULL DEFAULT 'strict' CHECK(policy IN ('strict','skip_issues')),
    parser_version text NOT NULL DEFAULT '1',
    preview_session_id uuid REFERENCES sessions,
    preview_data_revision bigint,
    preview_rules_epoch bigint,
    preview_hash text,
    confirmation_token_hash text,
    token_expires_at timestamptz,
    counts_json jsonb NOT NULL DEFAULT '{}',
    receipt_json jsonb,
    source_refs jsonb NOT NULL DEFAULT '[]',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    committed_at timestamptz
);
CREATE INDEX imports_owner ON invoice_import_batches(user_id,company_id,created_at);
CREATE TABLE invoice_import_rows (
    batch_id uuid REFERENCES invoice_import_batches ON DELETE CASCADE,
    row_number integer NOT NULL,
    normalized_values jsonb NOT NULL,
    row_hash text NOT NULL,
    classification text NOT NULL CHECK(classification IN ('new','duplicate_existing','duplicate_in_file','conflict','invalid')),
    reason_code text,
    reason_message text,
    comparison_revision bigint,
    compared_values jsonb,
    PRIMARY KEY(batch_id,row_number)
);
INSERT INTO schema_migrations(version) VALUES(3);
