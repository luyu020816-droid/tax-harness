CREATE TABLE IF NOT EXISTS schema_migrations (
    version integer PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE users (
    user_id text PRIMARY KEY,
    display_name text NOT NULL,
    next_source_seq bigint NOT NULL DEFAULT 0,
    memory_epoch bigint NOT NULL DEFAULT 0
);
CREATE TABLE companies (
    company_id text PRIMARY KEY,
    name text NOT NULL,
    aliases jsonb NOT NULL DEFAULT '[]'
);
CREATE TABLE user_company_grants (
    user_id text REFERENCES users,
    company_id text REFERENCES companies,
    can_read boolean NOT NULL,
    can_update boolean NOT NULL,
    PRIMARY KEY(user_id,company_id),
    CHECK (NOT can_update OR can_read)
);
CREATE TABLE company_heads (
    company_id text PRIMARY KEY REFERENCES companies,
    data_revision bigint NOT NULL DEFAULT 1,
    rules_epoch bigint NOT NULL DEFAULT 1
);
CREATE TABLE invoices (
    company_id text REFERENCES companies,
    invoice_id text,
    invoice_date date NOT NULL,
    seller text NOT NULL,
    item text NOT NULL,
    amount numeric(18,2) NOT NULL CHECK (amount >= 0),
    purpose text,
    note text,
    revision bigint NOT NULL DEFAULT 1,
    PRIMARY KEY(company_id,invoice_id)
);
CREATE INDEX invoices_month ON invoices(company_id,invoice_date);
CREATE TABLE bank_entries (
    company_id text REFERENCES companies,
    bank_id text,
    transaction_date date NOT NULL,
    direction text NOT NULL CHECK (direction IN ('income','expense')),
    amount numeric(18,2) NOT NULL CHECK (amount > 0),
    counterparty text NOT NULL,
    note text,
    PRIMARY KEY(company_id,bank_id)
);
CREATE INDEX bank_month ON bank_entries(company_id,transaction_date);
CREATE TABLE import_batches (
    batch_id uuid PRIMARY KEY,
    source_hash text NOT NULL UNIQUE,
    baseline_id text NOT NULL,
    row_counts jsonb NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE rule_versions (
    rule_version_id uuid PRIMARY KEY,
    rule_code text NOT NULL,
    display_name text NOT NULL,
    business_version text NOT NULL,
    effective_from date NOT NULL,
    effective_to date,
    status text NOT NULL CHECK (status IN ('draft','active','disabled')),
    topic text NOT NULL,
    full_text text NOT NULL,
    condition_json jsonb NOT NULL,
    source_ref text NOT NULL,
    content_revision bigint NOT NULL DEFAULT 1,
    UNIQUE(rule_code,business_version),
    CHECK(effective_to IS NULL OR effective_to > effective_from)
);
CREATE TABLE rule_scopes (
    rule_version_id uuid REFERENCES rule_versions,
    company_id text REFERENCES companies,
    PRIMARY KEY(rule_version_id,company_id)
);
CREATE TABLE login_sessions (
    token_hash text PRIMARY KEY,
    user_id text NOT NULL REFERENCES users,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE sessions (
    session_id uuid PRIMARY KEY,
    user_id text NOT NULL REFERENCES users,
    selected_company_id text REFERENCES companies,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE turns (
    turn_id uuid PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES sessions,
    user_id text NOT NULL REFERENCES users,
    client_request_id text NOT NULL,
    request_hash text NOT NULL,
    source_seq bigint NOT NULL,
    text text NOT NULL,
    status text NOT NULL DEFAULT 'queued',
    pending_clarification jsonb,
    continuation_of_turn_id uuid REFERENCES turns,
    resolved_by_turn_id uuid REFERENCES turns,
    active_run_generation bigint NOT NULL DEFAULT 0,
    next_event_seq bigint NOT NULL DEFAULT 0,
    final_response_ref uuid,
    final_message text,
    final_cards jsonb NOT NULL DEFAULT '[]',
    error jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(user_id,client_request_id)
);
CREATE INDEX turns_user_status ON turns(user_id,status,source_seq);
CREATE TABLE sources (
    source_id uuid PRIMARY KEY,
    turn_id uuid REFERENCES turns,
    user_id text NOT NULL REFERENCES users,
    role text NOT NULL,
    kind text NOT NULL,
    scope_kind text NOT NULL CHECK(scope_kind IN ('user_private','company_scoped')),
    content text NOT NULL,
    revision bigint NOT NULL DEFAULT 1,
    content_hash text NOT NULL,
    source_seq bigint NOT NULL,
    recall_state text NOT NULL DEFAULT 'active',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX sources_order ON sources(user_id,source_seq);
CREATE TABLE source_scopes (
    source_id uuid REFERENCES sources,
    company_id text REFERENCES companies,
    PRIMARY KEY(source_id,company_id)
);
CREATE TABLE work_items (
    work_id uuid PRIMARY KEY,
    user_id text NOT NULL REFERENCES users,
    source_id uuid NOT NULL REFERENCES sources,
    source_revision bigint NOT NULL DEFAULT 1,
    processor_version text NOT NULL DEFAULT '1',
    kind text NOT NULL,
    state text NOT NULL DEFAULT 'ready',
    attempts integer NOT NULL DEFAULT 0,
    lease_until timestamptz,
    fencing_token bigint NOT NULL DEFAULT 0,
    last_error jsonb,
    UNIQUE(source_id,source_revision,kind,processor_version)
);
CREATE INDEX work_ready ON work_items(state,lease_until);
CREATE TABLE source_assessments (
    source_id uuid REFERENCES sources,
    revision bigint NOT NULL,
    purpose text NOT NULL,
    processor_version text NOT NULL,
    state text NOT NULL,
    result_refs jsonb NOT NULL DEFAULT '[]',
    error jsonb,
    PRIMARY KEY(source_id,revision,purpose,processor_version)
);
CREATE TABLE source_progress (
    user_id text REFERENCES users,
    purpose text,
    contiguous_seq bigint NOT NULL DEFAULT 0,
    unresolved_refs jsonb NOT NULL DEFAULT '[]',
    PRIMARY KEY(user_id,purpose)
);
CREATE TABLE runs (
    run_id uuid PRIMARY KEY,
    turn_id uuid NOT NULL REFERENCES turns,
    generation bigint NOT NULL,
    state text NOT NULL,
    config_version text NOT NULL DEFAULT '1',
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    final_source_id uuid REFERENCES sources,
    UNIQUE(turn_id,generation)
);
CREATE TABLE run_events (
    turn_id uuid REFERENCES turns,
    event_seq bigint,
    run_id uuid REFERENCES runs,
    kind text NOT NULL,
    tool_call_id text,
    input_ref uuid,
    result_ref uuid REFERENCES sources,
    payload jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(turn_id,event_seq)
);
CREATE TABLE context_snapshots (
    context_id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES runs,
    model_step integer NOT NULL,
    payload jsonb NOT NULL,
    payload_hash text NOT NULL,
    manifest jsonb NOT NULL,
    token_count integer NOT NULL,
    count_kind text NOT NULL,
    usage jsonb,
    duration_ms integer,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(run_id,model_step)
);
CREATE TABLE tool_executions (
    run_id uuid REFERENCES runs,
    tool_call_id text,
    name text NOT NULL,
    arguments jsonb NOT NULL,
    arguments_hash text NOT NULL,
    state text NOT NULL,
    operation_id uuid,
    result_source_id uuid REFERENCES sources,
    PRIMARY KEY(run_id,tool_call_id)
);
CREATE TABLE tasks (
    task_id uuid PRIMARY KEY,
    user_id text NOT NULL REFERENCES users,
    company_id text NOT NULL REFERENCES companies,
    period text,
    intent text NOT NULL CHECK(intent IN ('query_month','explain_rules','complete_purposes')),
    status text NOT NULL CHECK(status IN ('active','waiting_user','completed','cancelled','replaced','deleted')),
    generation bigint NOT NULL,
    revision bigint NOT NULL DEFAULT 1,
    authority_source_seq bigint NOT NULL,
    unresolved_json jsonb NOT NULL DEFAULT '{}',
    last_check_ref uuid REFERENCES sources,
    checked_data_revision bigint,
    checked_rules_epoch bigint,
    deleted_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK(status NOT IN ('active','completed') OR period IS NOT NULL)
);
CREATE UNIQUE INDEX tasks_one_unfinished ON tasks(user_id,company_id)
    WHERE status IN ('active','waiting_user');
CREATE TABLE task_slots (
    user_id text REFERENCES users,
    company_id text REFERENCES companies,
    current_task_id uuid REFERENCES tasks,
    generation bigint NOT NULL DEFAULT 0,
    revision bigint NOT NULL DEFAULT 0,
    authority_source_seq bigint NOT NULL DEFAULT 0,
    PRIMARY KEY(user_id,company_id)
);
CREATE TABLE task_revisions (
    task_id uuid REFERENCES tasks,
    revision bigint,
    change_type text NOT NULL,
    snapshot jsonb NOT NULL,
    source_refs jsonb NOT NULL,
    source_seq bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(task_id,revision)
);
CREATE TABLE preferences (
    preference_id uuid PRIMARY KEY,
    user_id text NOT NULL REFERENCES users,
    scope_key text NOT NULL,
    stable_key text NOT NULL,
    value text,
    status text NOT NULL CHECK(status IN ('active','deleted')),
    generation bigint NOT NULL DEFAULT 1,
    revision bigint NOT NULL DEFAULT 1,
    authority_source_seq bigint NOT NULL,
    source_refs jsonb NOT NULL,
    deleted_at timestamptz,
    UNIQUE(user_id,scope_key,stable_key)
);
CREATE TABLE preference_revisions (
    preference_id uuid REFERENCES preferences,
    generation bigint,
    revision bigint,
    value text,
    status text NOT NULL,
    source_refs jsonb NOT NULL,
    source_seq bigint NOT NULL,
    PRIMARY KEY(preference_id,generation,revision)
);
CREATE TABLE memory_dependencies (
    dependent_type text,
    dependent_id uuid,
    dependent_revision bigint,
    source_type text,
    source_id uuid,
    source_revision bigint,
    source_generation bigint NOT NULL DEFAULT 1,
    PRIMARY KEY(dependent_type,dependent_id,dependent_revision,source_type,source_id)
);
CREATE TABLE summaries (
    summary_id uuid PRIMARY KEY,
    user_id text NOT NULL REFERENCES users,
    scope text NOT NULL,
    covered_sources jsonb NOT NULL,
    memory_epoch bigint NOT NULL,
    task_revision bigint,
    content text NOT NULL,
    status text NOT NULL,
    generation bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE history_index (
    source_id uuid REFERENCES sources,
    source_revision bigint,
    scope text NOT NULL,
    searchable_text text NOT NULL,
    index_version text NOT NULL DEFAULT '1',
    PRIMARY KEY(source_id,source_revision)
);
CREATE TABLE memory_change_ops (
    change_id uuid PRIMARY KEY,
    user_id text NOT NULL REFERENCES users,
    request_id text NOT NULL,
    request_hash text NOT NULL,
    target_type text NOT NULL,
    target_id uuid,
    expected_revision bigint,
    action text NOT NULL,
    state text NOT NULL,
    result_ref jsonb,
    UNIQUE(user_id,request_id)
);
CREATE TABLE result_views (
    view_id uuid PRIMARY KEY,
    user_id text NOT NULL REFERENCES users,
    session_id uuid NOT NULL REFERENCES sessions,
    company_id text NOT NULL REFERENCES companies,
    period text,
    ordered_keys jsonb NOT NULL,
    data_revision bigint NOT NULL,
    rules_epoch bigint NOT NULL,
    result_source_id uuid REFERENCES sources,
    displayed_event_seq bigint,
    status text NOT NULL DEFAULT 'active'
);
CREATE TABLE operations (
    operation_id uuid PRIMARY KEY,
    user_id text NOT NULL REFERENCES users,
    company_id text NOT NULL REFERENCES companies,
    invoice_id text NOT NULL,
    source_id uuid NOT NULL REFERENCES sources,
    intent_key text NOT NULL UNIQUE,
    request_hash text NOT NULL,
    before_revision bigint NOT NULL,
    after_revision bigint,
    patch jsonb NOT NULL,
    before_values jsonb,
    after_values jsonb,
    state text NOT NULL,
    verification_state text NOT NULL DEFAULT 'pending',
    last_error jsonb,
    committed_at timestamptz,
    FOREIGN KEY(company_id,invoice_id) REFERENCES invoices(company_id,invoice_id),
    UNIQUE(user_id,source_id,company_id,invoice_id)
);
INSERT INTO schema_migrations(version) VALUES(1);

