CREATE TABLE scope_bindings (
    binding_id uuid PRIMARY KEY,
    user_id text NOT NULL REFERENCES users ON DELETE CASCADE,
    turn_id uuid NOT NULL REFERENCES turns ON DELETE CASCADE,
    company_id text NOT NULL REFERENCES companies ON DELETE CASCADE,
    period text NOT NULL CHECK(period ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'),
    basis_json jsonb NOT NULL,
    basis_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(turn_id,company_id,period,basis_hash)
);

CREATE TABLE write_plans (
    plan_id uuid PRIMARY KEY,
    user_id text NOT NULL REFERENCES users ON DELETE CASCADE,
    session_id uuid REFERENCES sessions ON DELETE CASCADE,
    origin_source_id uuid NOT NULL REFERENCES sources ON DELETE CASCADE,
    origin_source_revision bigint NOT NULL,
    authorized_source_id uuid REFERENCES sources,
    company_id text NOT NULL REFERENCES companies ON DELETE CASCADE,
    invoice_id text NOT NULL,
    patch jsonb NOT NULL CHECK(patch <> '{}'::jsonb AND patch - 'purpose' - 'note' = '{}'::jsonb),
    before_values jsonb NOT NULL,
    expected_invoice_revision bigint NOT NULL CHECK(expected_invoice_revision > 0),
    selection_ref jsonb,
    data_revision bigint NOT NULL,
    rules_epoch bigint NOT NULL,
    intent_hash text NOT NULL,
    revision bigint NOT NULL DEFAULT 1,
    status text NOT NULL CHECK(status IN ('authorized','needs_confirmation','prohibited','consumed','revoked','expired')),
    authorization_kind text CHECK(authorization_kind IN ('direct_command','user_confirmation')),
    verifier_version text NOT NULL DEFAULT 'direct-v1',
    expires_at timestamptz NOT NULL DEFAULT now()+interval '10 minutes',
    confirmation_turn_id uuid REFERENCES turns,
    operation_id uuid REFERENCES operations,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(user_id,origin_source_id,company_id,invoice_id),
    FOREIGN KEY(company_id,invoice_id) REFERENCES invoices(company_id,invoice_id)
);
ALTER TABLE turns ADD COLUMN request_kind text NOT NULL DEFAULT 'chat'
    CHECK(request_kind IN ('chat','write_confirmation'));
ALTER TABLE turns ADD COLUMN control_ref uuid REFERENCES write_plans;
ALTER TABLE operations ADD COLUMN authorization_plan_id uuid REFERENCES write_plans;

-- Apply with old workers stopped. Fence saved legacy payloads rather than replay
-- an obsolete free-patch/message protocol under the new authorization contract.
UPDATE work_items SET state='cancelled',fencing_token=fencing_token+1,lease_until=NULL
WHERE kind='turn' AND state IN ('ready','leased','running') AND source_id IN (
    SELECT s.source_id FROM sources s JOIN turns t USING(turn_id)
    WHERE t.status IN ('queued','running')
);
UPDATE runs SET state='interrupted',finished_at=now() WHERE state='running';
UPDATE turns SET status='interrupted',active_run_generation=active_run_generation+1,
    error='{"code":"PROTOCOL_UPGRADED","message":"执行协议已升级，请重新提交未完成的请求。"}'
WHERE status IN ('queued','running');

INSERT INTO schema_migrations(version) VALUES(4);
