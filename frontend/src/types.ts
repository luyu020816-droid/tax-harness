export interface Company {
  company_id: string;
  name: string;
  aliases: string[];
  can_update: boolean;
  can_import_invoices: boolean;
  can_upload_knowledge: boolean;
}
export interface User {
  user_id: string;
  display_name: string;
  companies: Company[];
  model_ready: boolean;
  dataset_profile: 'competition' | 'extended';
  invoice_import_ready: boolean;
  model_mode: string;
}
export interface Session {
  session_id: string;
  selected_company_id: string | null;
  title?: string;
  updated_at: string;
}
export interface Invoice {
  invoice_id: string;
  company_id: string;
  invoice_date: string;
  amount: string;
  purpose: string | null;
  note: string | null;
  seller: string;
  item: string;
  revision: number;
}
export interface Rule {
  rule_version_id: string;
  rule_code: string;
  display_name: string;
  business_version: string;
  full_text: string;
  status: string;
  effective_from: string;
  effective_to: string | null;
  content_revision: number;
}
interface CardBase {
  company_id: string;
  period?: string;
  as_of?: string;
  rule_refs?: Rule[];
}
export interface SummaryCard extends CardBase {
  kind: 'monthly_summary';
  invoice_total: string;
  bank_income: string;
  bank_expense: string;
  invoice_count: number;
  bank_count: number;
  income_count: number;
  expense_count: number;
  has_records: boolean;
}
export interface ListCard extends CardBase {
  kind: 'purpose_check' | 'invoices';
  rows: Invoice[];
  total: number;
  missing_count?: number;
  required_count?: number;
  offset: number;
  has_more: boolean;
  next_cursor: string | null;
  view_id?: string;
}
export interface OperationCard extends CardBase {
  kind: 'operation';
  operation_id: string;
  invoice_id: string;
  commit_state: string;
  verification_state: string;
  patch: Record<string, string | null>;
  saved_values: Record<string, string | null> | null;
  current_values?: Record<string, string | null>;
}
export interface RulesCard extends CardBase {
  kind: 'rules';
  rules: Rule[];
}
export interface InvoiceWritePlan extends CardBase {
  kind: 'write_plan';
  plan_id: string;
  invoice_id: string;
  patch: Record<string, string | null>;
  before_values: Record<string, string | null>;
  revision: number;
  intent_hash: string;
  status: 'authorized' | 'needs_confirmation' | 'prohibited' | 'consumed' | 'revoked' | 'expired';
  expires_at: string;
  operation_id: string | null;
  confirmation_turn_id: string | null;
}
export type ResultCard =
  | SummaryCard
  | ListCard
  | OperationCard
  | RulesCard
  | ImportBatch
  | InvoiceWritePlan
  | KnowledgeQuoteCard;
export interface Turn {
  request_kind?: 'chat' | 'write_confirmation';
  turn_id: string;
  source_seq: number;
  text: string;
  status: string;
  final_message: string | null;
  final_cards: ResultCard[];
  next_event_seq: number;
  selected_company_id?: string | null;
  error: { code: string; message: string } | null;
  runs?: { run_id: string; state: string }[];
}
export interface Task {
  task_id: string;
  company_id: string;
  period: string | null;
  intent: string;
  status: string;
  revision: number;
  generation: number;
  checked_data_revision: number | null;
  last_check_ref: string | null;
  snapshot_current?: boolean;
  unresolved_json: {
    missing_count?: number;
    needs_refresh?: boolean;
    question?: string;
    missing_fields?: string[];
  };
  updated_at: string;
}
export interface Preference {
  preference_id: string;
  scope_key: string;
  stable_key: string;
  value: string;
  revision: number;
  generation: number;
  source_refs: { source_id: string }[];
}
export interface Pending {
  turn_id: string;
  pending_clarification: {
    summary: string;
    company_id: string | null;
    period: string | null;
    revision: number;
    task_id?: string | null;
  };
}
export interface RunSummary {
  run_id: string;
  text: string;
  state: string;
  started_at: string;
  finished_at: string | null;
}
export interface RunDetail {
  run: RunSummary;
  contexts: {
    context_id: string;
    model_step: number;
    payload: unknown;
    manifest: { kind?: string; [key: string]: unknown };
    token_count: number;
    count_kind: string;
    duration_ms: number | null;
  }[];
  tools: {
    tool_call_id: string;
    name: string;
    arguments: unknown;
    actual_result: string;
    state: string;
  }[];
  events: unknown[];
}
export interface Source {
  source_id: string;
  role: string;
  kind: string;
  content: string;
  recall_state: string;
  created_at: string;
}
export type Detail =
  | { kind: 'import'; id?: string }
  | { kind: 'rule'; data: Rule }
  | { kind: 'run'; id: string }
  | { kind: 'task'; data: Task }
  | { kind: 'source'; id: string }
  | { kind: 'knowledge'; id: string }
  | { kind: 'evidence'; id: string };

export interface KnowledgeVersion {
  version_id: string;
  file_id: string;
  edition: number;
  state: string;
  original_name: string;
  job_id: string;
  stage: string;
  effective_from: string;
  effective_to: string | null;
  error: { code: string; message: string } | null;
}
export interface KnowledgeDocument {
  document_id: string;
  company_id: string;
  owner_id: string;
  scope: 'user_private' | 'company';
  title: string;
  revision: number;
  status: string;
  active_version_id: string | null;
  active_edition?: number | null;
  latest?: {
    version_id: string;
    edition: number;
    state: string;
    job_id: string;
    error: { message: string } | null;
  };
  versions?: KnowledgeVersion[];
}
export interface KnowledgeAttachment {
  file_id: string;
  document_id: string;
  company_id: string;
  title: string;
  original_name: string;
}
export interface KnowledgeEvidence {
  citation_id: string;
  document_id: string;
  version_id: string;
  company_id: string;
  title: string;
  edition: number;
  excerpt: string;
  locator: Record<string, string | number | boolean>[];
}
export interface KnowledgeBundle {
  query_id: string;
  status: string;
  retrieval_mode: string;
  degraded_reason?: string | null;
  evidence: KnowledgeEvidence[];
  coverage: { eligible: number; ready: number; pending: number };
}
export interface KnowledgeQuoteCard extends CardBase {
  kind: 'knowledge_quote';
  citation_id: string;
  title: string;
  edition: number;
  locator_label: string;
}

export interface UploadedInvoice {
  upload_id: string;
  original_name: string;
  company_id: string;
  file_sha256: string;
}
export interface ImportCounts {
  new?: number;
  duplicate_existing?: number;
  duplicate_in_file?: number;
  conflict?: number;
  invalid?: number;
  total?: number;
  new_amount?: string;
  error?: { code: string; message: string };
}
export interface ImportBatch extends CardBase {
  kind: 'import_preview' | 'import_receipt';
  batch_id: string;
  upload_id: string;
  revision: number;
  status: string;
  policy: string;
  preview_hash: string | null;
  counts: ImportCounts;
  confirmation_token?: string;
  confirmation_expired: boolean;
  receipt: { inserted_count: number; skipped_count: number; after_data_revision: number } | null;
  rows: {
    row_number: number;
    normalized_values: Partial<Invoice>;
    classification: 'new' | 'duplicate_existing' | 'duplicate_in_file' | 'conflict' | 'invalid';
    reason_message: string | null;
  }[];
  created_at: string;
  committed_at: string | null;
  has_more: boolean;
  next_cursor: number | null;
}
export interface ImportSummary {
  batch_id: string;
  company_id: string;
  status: string;
  revision: number;
  original_name: string;
  created_at: string;
}
