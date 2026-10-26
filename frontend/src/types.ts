export interface Company {
  company_id: string;
  name: string;
  aliases: string[];
  can_update: boolean;
}
export interface User {
  user_id: string;
  display_name: string;
  companies: Company[];
  model_ready: boolean;
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
export type ResultCard = SummaryCard | ListCard | OperationCard | RulesCard;
export interface Turn {
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
    manifest: unknown;
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
  | { kind: 'rule'; data: Rule }
  | { kind: 'run'; id: string }
  | { kind: 'task'; data: Task }
  | { kind: 'source'; id: string };
