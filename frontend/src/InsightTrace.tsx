import { useEffect, useState } from 'react';
import { ArrowLeft, ChevronDown, ChevronRight, Download } from 'lucide-react';
import { api, dateTime, statusName } from './api';
import { duration, number, fee } from './insight-format';
import { useInsight } from './useInsight';
import type { InsightDetail, InsightSpan } from './insight-types';
const spanName: Record<string, string> = {
  context: '上下文',
  model: '模型调用',
  tool: '工具执行',
  retrieval: '资料检索',
  write_verify: '保存核验',
  host: '宿主处理',
};
const spanState: Record<string, string> = {
  succeeded: '成功',
  failed: '失败',
  running: '进行中',
  cancelled: '已取消',
  interrupted: '中断 / 结束未知',
  not_dispatched: '未发出',
};
const toolBlocks: Record<string, { label: string; message: string }> = {
  NEEDS_SCOPE_CLARIFICATION: {
    label: '需确认范围',
    message: '工具已拦截，请确认企业或年月后再查询。',
  },
  ACCESS_DENIED: {
    label: '无权限',
    message: '当前身份无权执行此操作，工具已拦截。',
  },
};
const attemptName: Record<string, string> = {
  normal: '模型请求',
  summary: '生成历史摘要',
  repair: '修复摘要格式',
};
const partName: Record<string, string> = {
  system_instruction: '系统指令与反馈',
  task_and_preferences: '任务与用户偏好',
  scope_and_state: '企业范围与正式状态',
  history: '历史与模型消息',
  current_input: '当前用户输入',
  current_material: '本次请求资料（宿主整理）',
  retrieval_results: '历史与知识检索结果',
  rule_results: '规则结果',
  protocol_other: '协议与未归属开销',
  combined_payload: '完整输入（无法细分）',
  system_and_state: '指令与正式状态',
  history_and_current: '对话与当前输入',
  summary: '历史摘要',
  tool_results: '工具结果',
  tool_definitions: '工具定义',
};

function RawNode({ id }: { id: string }) {
  const [offset, setOffset] = useState(0);
  const node = useInsight<{
    text?: string;
    redacted: boolean;
    reason?: string;
    next_offset?: number | null;
    total_bytes?: number;
  }>(`/insights/spans/${id}?offset=${offset}`);
  return (
    <div className="insight-raw">
      {node.loading && <p className="muted">读取受控原始记录…</p>}
      {node.error && <p className="error-note">{node.error}</p>}
      {node.data?.redacted ? (
        <p className="notice">{node.data.reason}</p>
      ) : (
        <>
          <p className="muted small">
            原始 JSON 文本 · 第 {offset.toLocaleString()} 字节起 · 总计{' '}
            {node.data?.total_bytes?.toLocaleString() ?? '…'} 字节
          </p>
          <pre>{node.data?.text}</pre>
          <div className="form-actions">
            <button className="secondary" disabled={!offset} onClick={() => setOffset(0)}>
              回到开头
            </button>
            <button
              className="secondary"
              disabled={node.data?.next_offset == null}
              onClick={() => setOffset(node.data?.next_offset ?? 0)}
            >
              继续读取
            </button>
          </div>
        </>
      )}
    </div>
  );
}

function SpanNode({
  span,
  maximum,
  nested,
  canRead,
  offsetMs = 0,
}: {
  span: InsightSpan;
  maximum: number;
  nested: boolean;
  canRead: boolean;
  offsetMs?: number;
}) {
  const [open, setOpen] = useState(span.status === 'failed');
  const [raw, setRaw] = useState(false);
  const blocked =
    span.kind === 'tool' && span.status === 'failed' && span.error_code
      ? toolBlocks[span.error_code]
      : undefined;
  const usage = span.attributes.usage;
  const parts = span.attributes.input_composition;
  return (
    <article
      className={`insight-span ${nested ? 'is-child' : ''} ${span.status === 'failed' && !blocked ? 'is-error' : ''}`}
    >
      <button className="insight-span-toggle" aria-expanded={open} onClick={() => setOpen(!open)}>
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        <span className="insight-node-dot" />
        <span className="insight-node-title">
          <small>{spanName[span.kind] ?? span.kind}</small>
          <strong>{attemptName[span.name] ?? span.name}</strong>
        </span>
        <span className="insight-node-status">
          {blocked?.label ?? spanState[span.status] ?? span.status}
        </span>
        <span className="insight-duration">{duration(span.duration_ms)}</span>
      </button>
      <div className="insight-time-track" aria-hidden="true">
        <i
          style={{
            marginLeft: `${Math.min(100, (offsetMs / maximum) * 100)}%`,
            width: `${span.duration_ms == null ? 0 : Math.max(0.5, Math.min(100 - (offsetMs / maximum) * 100, (span.duration_ms / maximum) * 100))}%`,
          }}
        />
      </div>
      {open && (
        <div className="insight-span-content">
          <p className="muted small">
            {span.measurement_kind === 'measured'
              ? '实际采集'
              : span.measurement_kind === 'event_derived'
                ? '由历史记录推导'
                : '未完整采集'}
            {span.started_at && ` · ${dateTime(span.started_at)}`}
          </p>
          {span.attributes.model && <p className="small">模型：{span.attributes.model}</p>}
          {canRead && span.attributes.retrieval && (
            <details className="insight-evidence">
              <summary>聊天检索诊断</summary>
              {span.attributes.retrieval.profile_id && (
                <p className="small" title={span.attributes.retrieval.profile_id}>
                  索引版本：{span.attributes.retrieval.profile_id.slice(0, 12)}…
                </p>
              )}
              {span.attributes.retrieval.policy_hash && (
                <p className="small" title={span.attributes.retrieval.policy_hash}>
                  策略版本：{span.attributes.retrieval.policy_hash.slice(0, 12)}…
                </p>
              )}
              <div className="insight-inline-metrics">
                {span.attributes.retrieval.candidate_count != null && (
                  <span>
                    候选 <b>{span.attributes.retrieval.candidate_count}</b>
                  </span>
                )}
                {span.attributes.retrieval.accepted_count != null && (
                  <span>
                    通过支持度 <b>{span.attributes.retrieval.accepted_count}</b>
                  </span>
                )}
                {span.attributes.retrieval.coverage && (
                  <span>
                    已索引 {span.attributes.retrieval.coverage.indexed ?? '未知'} /{' '}
                    {span.attributes.retrieval.coverage.eligible ?? '未知'}
                  </span>
                )}
              </div>
              <p className="muted small">
                本地推理 · Token {span.attributes.retrieval.local_tokens ?? '未测量'} ·
                不计入远端模型API费用
              </p>
            </details>
          )}
          {span.kind === 'model' && (
            <p className="muted small">
              计价：
              {span.attributes.price_snapshot
                ? `${span.attributes.price_snapshot.as_of} · ${span.attributes.price_snapshot.currency} 快照估算`
                : span.attributes.price_captured
                  ? '调用时未匹配价格，费用未知'
                  : '历史未记录价格，可按明确配置的快照归一化估算'}
            </p>
          )}
          {span.attributes.operation_count != null && (
            <p className="small">
              恢复前已查找 {span.attributes.operation_count}{' '}
              笔旧操作；后续按实际回执或当前授权继续。
            </p>
          )}
          {span.kind === 'model' && (
            <div className="insight-inline-metrics">
              <span>
                输入 <b>{usage?.prompt_tokens == null ? '未知' : number(usage.prompt_tokens)}</b>
              </span>
              <span>
                输出{' '}
                <b>{usage?.completion_tokens == null ? '未知' : number(usage.completion_tokens)}</b>
              </span>
              <span>Token 以供应商 usage 为准</span>
            </div>
          )}
          {blocked ? (
            <p className="notice" title={span.error_code ?? undefined}>
              {blocked.message}
            </p>
          ) : (
            span.error_code && <p className="error-note">{span.error_code}</p>
          )}
          {parts && (
            <details className="insight-evidence">
              <summary>输入构成 · UTF-8 预算估算</summary>
              <dl>
                {Object.entries(parts.parts)
                  .filter(([, v]) => v > 0)
                  .map(([k, v]) => (
                    <div key={k}>
                      <dt>{partName[k] ?? k}</dt>
                      <dd>{Math.round(v).toLocaleString()} 估算单位</dd>
                    </div>
                  ))}
              </dl>
              <p className="muted small">{parts.note}</p>
            </details>
          )}
          {!canRead ? (
            <p className="muted">原文来源已失效，保留计量，隐藏内容。</p>
          ) : (
            span.span_id && (
              <>
                <button className="text-button" onClick={() => setRaw(!raw)}>
                  {raw ? '收起原始记录' : '查看实际输入 / 工具结果'}
                </button>
                {raw && <RawNode id={span.span_id} />}
              </>
            )
          )}
        </div>
      )}
    </article>
  );
}

type Evidence = {
  redacted?: boolean;
  reason?: string;
  current_rules?: { rule_version_id: string; status: string; content_revision: number }[];
  performed_delivery?: { kind: string; company_id: string; period: string; status: string }[];
  contexts?: {
    context_id: string;
    summary_id?: string | null;
    retrieval_refs?: string[] | null;
    sources?: { source_id: string; revision: number }[] | null;
    rule_refs?:
      | {
          rule_version_id?: string;
          display_name?: string;
          business_version?: string;
          content_revision?: number;
        }[]
      | null;
  }[];
  operations?: {
    operation_id: string;
    company_id: string;
    invoice_id: string;
    before_revision: number;
    after_revision: number | null;
    state: string;
    verification_state: string;
  }[];
};
function EvidencePanel({ value }: { value: Record<string, unknown> }) {
  const e = value as Evidence;
  if (e.redacted) return <p className="notice">{e.reason}</p>;
  const contexts = e.contexts ?? [];
  const currentRules = new Map(e.current_rules?.map((r) => [r.rule_version_id, r]) ?? []);
  return (
    <div className="insight-evidence-panel">
      <p className="muted small">
        展示真实来源与回执关联；来源齐全不证明语义无损，未标注事项仍待核验。
      </p>
      <h4>已执行事项的交付</h4>
      <p className="muted small">只核对实际执行记录，用户全部子需求是否被识别仍未核验。</p>
      {e.performed_delivery?.map((d, i) => (
        <div className="insight-proof" key={i}>
          <strong>
            {d.company_id} · {d.period} · {d.kind === 'monthly_summary' ? '月度查询' : '用途检查'}
          </strong>
          <span>{d.status === 'delivered' ? '已交付结果' : '已执行，尚未交付'}</span>
        </div>
      ))}
      <h4>请求级保存与核验</h4>
      <p className="muted small">
        以下为关联账本，不据此断言当前执行提交了保存；请结合提交时间及实际工具节点核对。
      </p>
      {!e.operations?.length && <p className="muted small">此请求没有已记录的发票保存操作。</p>}
      {e.operations?.map((o) => (
        <div className="insight-proof" key={o.operation_id}>
          <strong>
            {o.company_id} · {o.invoice_id}
          </strong>
          <span>
            修订 {o.before_revision} → {o.after_revision ?? '未知'} · {o.state}
          </span>
          <span>
            回读核验：{o.verification_state === 'verified' ? '已核验' : o.verification_state}
          </span>
          <code>{o.operation_id}</code>
        </div>
      ))}
      <h4>实际入模依据</h4>
      {contexts.map((c) => (
        <div className="insight-proof" key={c.context_id}>
          <strong>输入快照 {c.context_id.slice(0, 8)}</strong>
          <span>
            {c.sources == null ? '来源未记录' : `${c.sources.length} 条来源`} ·{' '}
            {c.retrieval_refs == null
              ? '知识引用未记录'
              : `${c.retrieval_refs.length} 次知识检索引用`}
          </span>
          {c.summary_id && <span>历史摘要 {c.summary_id}</span>}
          {c.rule_refs?.map((r, i) => (
            <span key={`${r.rule_version_id}-${i}`}>
              {r.display_name ?? r.rule_version_id ?? '规则'} · 当时{' '}
              {r.business_version ?? '版本未记录'} · 当前
              {currentRules.get(r.rule_version_id ?? '')?.status === 'active'
                ? '启用'
                : currentRules.has(r.rule_version_id ?? '')
                  ? '非启用'
                  : '状态未记录'}
            </span>
          ))}
          <details>
            <summary>来源修订</summary>
            {c.sources?.map((s, i) => (
              <p className="small" key={`${s.source_id}-${i}`}>
                <code>{s.source_id}</code> · 修订 {s.revision}
              </p>
            ))}
          </details>
        </div>
      ))}
      <details>
        <summary>原始证据清单 JSON</summary>
        <pre>{JSON.stringify(value, null, 2)}</pre>
      </details>
    </div>
  );
}

export function RequestInsight({ turnId, onBack }: { turnId: string; onBack?: () => void }) {
  const [revision, setRevision] = useState(0);
  const [extra, setExtra] = useState<Record<string, InsightSpan[]>>({});
  const [next, setNext] = useState<Record<string, number | null>>({});
  const [moreError, setMoreError] = useState('');
  const [moreLoading, setMoreLoading] = useState(false);
  async function more(runId: string, offset: number) {
    setMoreLoading(true);
    setMoreError('');
    try {
      const page = await api<{ spans: InsightSpan[]; next_offset: number | null }>(
        `/insights/runs/${runId}/spans?offset=${offset}`
      );
      setExtra((v) => ({ ...v, [runId]: [...(v[runId] ?? []), ...page.spans] }));
      setNext((v) => ({ ...v, [runId]: page.next_offset }));
    } catch (err) {
      setMoreError((err as Error).message);
    } finally {
      setMoreLoading(false);
    }
  }
  const { data, error, loading } = useInsight<InsightDetail>(
    `/insights/requests/${turnId}`,
    revision
  );
  const active = data && ['running', 'queued'].includes(data.request.status);
  useEffect(() => {
    if (!active) return;
    const refresh = () => {
      if (document.visibilityState === 'visible') setRevision((v) => v + 1);
    };
    const timer = window.setInterval(refresh, 2000);
    document.addEventListener('visibilitychange', refresh);
    return () => {
      clearInterval(timer);
      document.removeEventListener('visibilitychange', refresh);
    };
  }, [active]);
  if (error)
    return (
      <div className="insight-empty">
        <p className="error-note">{error}</p>
        <button className="secondary" onClick={() => setRevision((v) => v + 1)}>
          重试
        </button>
        {onBack && (
          <button className="text-button" onClick={onBack}>
            返回历史记录
          </button>
        )}
      </div>
    );
  if (!data) return <p className="muted">正在读取完整执行链路…</p>;
  const r = data.request;
  return (
    <section className="insight-detail" aria-label="请求执行轨迹" aria-busy={loading}>
      {onBack && (
        <button className="text-button insight-back" onClick={onBack}>
          <ArrowLeft size={14} />
          返回历史记录
        </button>
      )}
      <div className="insight-detail-heading">
        <span className={`badge ${r.status === 'completed' ? 'success' : 'quiet'}`}>
          {statusName(r.status)}
        </span>
        <a className="text-button" href={`/api/v1/insights/requests/${r.turn_id}/export`} download>
          <Download size={14} />
          导出轨迹
        </a>
      </div>
      <h2>{r.text}</h2>
      <p className="muted small">
        {dateTime(r.created_at)} · {r.models.join(' / ') || '模型未记录'}
      </p>
      <div className="insight-request-metrics">
        <span>
          活跃执行 <b>{duration(r.active_ms)}</b>
        </span>
        <span>
          请求历时 <b>{duration(r.elapsed_ms)}</b>
        </span>
        <span>
          首次排队 <b>{duration(r.queue_ms)}</b>
        </span>
        <span>
          执行 / 模型{' '}
          <b>
            {r.run_count} / {r.model_attempts} 次
          </b>
        </span>
        <span>
          已知 Token{' '}
          <b>
            {number(r.input_tokens)} 入 / {number(r.output_tokens)} 出
          </b>
        </span>
        <span>
          费用估算 <b>{fee(r.cost)}</b>
        </span>
      </div>
      <p className="insight-caveat">
        执行状态不代表全部业务正确。请求历时含等待与恢复间隔；没有独立预期的内容标为未核验。
        {r.active_time_note}
      </p>
      {!data.runs.length && <p className="empty-panel">请求尚未开始执行。</p>}
      {data.runs.map((run) => {
        const spans = [...run.spans, ...(extra[run.run_id] ?? [])];
        const max = Math.max(
          1,
          run.finished_at ? Date.parse(run.finished_at) - Date.parse(run.started_at) : 0,
          ...spans.map(
            (s) =>
              (s.started_at ? Date.parse(s.started_at) - Date.parse(run.started_at) : 0) +
              (s.duration_ms ?? 0)
          )
        );
        const nextOffset =
          next[run.run_id] === undefined
            ? (run.next_span_offset ?? (run.has_more_spans ? 100 : null))
            : next[run.run_id];
        return (
          <section className="insight-run" key={run.run_id}>
            <header>
              <strong>
                执行 {run.generation}
                {run.generation > 1 ? ' · 恢复 / 重试' : ''}
              </strong>
              <span className="badge quiet">{statusName(run.state)}</span>
            </header>
            <p className="muted small">
              {dateTime(run.started_at)} ·{' '}
              {run.finished_at
                ? duration(Date.parse(run.finished_at) - Date.parse(run.started_at))
                : '尚未记录结束'}
            </p>
            <div className="insight-chain">
              {spans.map((s, index) => (
                <SpanNode
                  key={s.span_id ?? `incomplete-${index}`}
                  span={s}
                  nested={!!s.parent_span_id}
                  maximum={max}
                  canRead={run.content_available}
                  offsetMs={
                    s.started_at
                      ? Math.max(0, Date.parse(s.started_at) - Date.parse(run.started_at))
                      : 0
                  }
                />
              ))}
            </div>
            {nextOffset != null && (
              <button
                className="secondary"
                disabled={moreLoading}
                onClick={() => void more(run.run_id, nextOffset)}
              >
                继续读取轨迹节点
              </button>
            )}
            <details className="insight-evidence">
              <summary>记忆、规则与业务依据 · 按需展开</summary>
              <EvidencePanel value={run.evidence} />
            </details>
            <details className="insight-evidence">
              <summary>版本与采集信息</summary>
              <pre>
                {JSON.stringify(run.insight_snapshot ?? { status: '历史未记录配置指纹' }, null, 2)}
              </pre>
            </details>
          </section>
        );
      })}
      {moreError && <p className="error-note">{moreError}</p>}
    </section>
  );
}

export function RunInsightDrawer({ runId }: { runId: string }) {
  const link = useInsight<{ turn_id: string }>(`/insights/runs/${runId}`);
  return link.error ? (
    <p className="error-note">{link.error}</p>
  ) : link.data ? (
    <RequestInsight turnId={link.data.turn_id} />
  ) : (
    <p className="muted">正在定位原请求…</p>
  );
}
