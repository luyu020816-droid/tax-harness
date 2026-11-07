export interface InsightCost {
  known_cost: string | null;
  complete_cost: string | null;
  unknown_calls: number;
  currency: string | null;
  pricing_versions: string[];
  upper_estimate_calls: number;
}
export interface InsightRequest {
  turn_id: string;
  text: string;
  status: string;
  created_at: string;
  run_count: number;
  model_attempts: number;
  usage_known_calls: number;
  unknown_dispatch_calls: number;
  input_tokens: number;
  output_tokens: number;
  active_ms: number | null;
  active_time_note?: string;
  elapsed_ms: number | null;
  queue_ms: number | null;
  models: string[];
  cost: InsightCost;
  content_available: boolean;
}
export interface InsightSummary {
  request_count: number;
  terminal_count: number;
  completion_ratio: number | null;
  p50_ms: number | null;
  p95_ms: number | null;
  timed_requests: number;
  small_sample: boolean;
  input_tokens: number;
  output_tokens: number;
  model_attempts: number;
  run_count: number;
  states: Record<string, number>;
  cost: InsightCost;
  measured_at: string;
  coverage: {
    usage_known: number;
    dispatched: number;
    dispatch_unknown: number;
    timed_calls: number;
    versioned_runs: number;
    runs: number;
    open_spans?: number;
    abandoned_spans?: number;
  };
}
export interface InsightSpan {
  span_id: string | null;
  parent_span_id?: string | null;
  kind: string;
  name: string;
  status: string;
  started_at: string | null;
  duration_ms: number | null;
  measurement_kind: string;
  error_code?: string | null;
  attributes: {
    model?: string;
    operation_count?: number | null;
    model_mode?: string | null;
    price_snapshot?: { as_of: string; currency: string; source: string } | null;
    price_captured?: boolean;
    retrieval?: {
      profile_id?: string | null;
      policy_hash?: string | null;
      candidate_count?: number | null;
      accepted_count?: number | null;
      invalidated_count?: number | null;
      text_count?: number | null;
      local_tokens?: number | null;
      token_measurement?: string | null;
      coverage?: { eligible?: number; indexed?: number; pending?: number };
    } | null;
    usage?: { prompt_tokens?: number; completion_tokens?: number } | null;
    input_composition?: { kind: string; parts: Record<string, number>; note: string };
  };
}
export interface InsightDetail {
  request: InsightRequest;
  runs: {
    run_id: string;
    generation: number;
    state: string;
    started_at: string;
    finished_at: string | null;
    content_available: boolean;
    insight_snapshot: Record<string, unknown> | null;
    spans: InsightSpan[];
    has_more_spans: boolean;
    next_span_offset?: number | null;
    evidence: Record<string, unknown>;
  }[];
}
