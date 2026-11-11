import { useCallback, useEffect, useRef, useState } from 'react';
import { Activity, ChevronRight, RefreshCw } from 'lucide-react';
import { dateTime, statusName } from './api';
import { duration, number, fee } from './insight-format';
import { useInsight } from './useInsight';
import { RequestInsight } from './InsightTrace';
import { Evaluations } from './InsightEvaluations';
import type { InsightRequest, InsightSummary } from './insight-types';
import './insights.css';
function todayDate() {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(new Date());
}
function shiftDate(date: string, days: number) {
  return new Date(Date.parse(`${date}T00:00:00Z`) + days * 86400000).toISOString().slice(0, 10);
}

export function InsightsView({ company }: { company: string | null }) {
  const initial = new URLSearchParams(location.search);
  const [from, setFrom] = useState(initial.get('insight_from') ?? shiftDate(todayDate(), -6));
  const [to, setTo] = useState(initial.get('insight_to') ?? todayDate());
  const [status, setStatus] = useState(initial.get('insight_status') ?? '');
  const [model, setModel] = useState(initial.get('insight_model') ?? '');
  const [selected, setSelected] = useState(
    initial.get('insight_company') === (company ?? '*') ? (initial.get('insight_turn') ?? '') : ''
  );
  const [cursor, setCursor] = useState('');
  const [previous, setPrevious] = useState<string[]>([]);
  const [ceiling, setCeiling] = useState(new Date().toISOString());
  const [revision, setRevision] = useState(0);
  const [comparison, setComparison] = useState(false);
  const tracePane = useRef<HTMLDivElement>(null);
  useEffect(() => {
    tracePane.current?.scrollTo(0, 0);
  }, [selected]);
  const valid = !!from && !!to && from <= to && Date.parse(to) - Date.parse(from) < 31 * 86400000;
  const params = new URLSearchParams({
    from: `${from}T00:00:00+08:00`,
    to: `${valid ? shiftDate(to, 1) : to}T00:00:00+08:00`,
    as_of: ceiling,
  });
  if (company) params.set('company_id', company);
  if (status) params.set('status', status);
  if (model.trim()) params.set('model', model.trim());
  const query = params.toString();
  const summary = useInsight<InsightSummary>(valid ? `/insights/summary?${query}` : null, revision);
  const list = useInsight<{
    requests: InsightRequest[];
    next_cursor: string | null;
    has_more: boolean;
  }>(
    valid
      ? `/insights/requests?${query}&limit=5${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`
      : null,
    revision
  );
  const filterKey = JSON.stringify([from, to, status, model, company]);
  const previousFilter = useRef(filterKey);
  useEffect(() => {
    if (previousFilter.current === filterKey) return;
    previousFilter.current = filterKey;
    setCursor('');
    setPrevious([]);
    setSelected('');
  }, [filterKey]);
  useEffect(() => {
    const p = new URLSearchParams(location.search);
    const values: Record<string, string> = {
      insight_from: from,
      insight_to: to,
      insight_status: status,
      insight_model: model,
      insight_turn: selected,
      insight_company: company ?? '*',
    };
    for (const [k, v] of Object.entries(values)) {
      if (v) p.set(k, v);
      else p.delete(k);
    }
    history.replaceState(null, '', `${location.pathname}?${p}`);
  }, [from, to, status, model, selected, company]);
  const refresh = useCallback(() => {
    setCursor('');
    setPrevious([]);
    setCeiling(new Date().toISOString());
    setRevision((v) => v + 1);
  }, []);
  const s = summary.data;
  if (comparison) return <Evaluations onBack={() => setComparison(false)} />;
  return (
    <section className="insights-page">
      <div className="page-heading">
        <div>
          <span className="insight-kicker">
            <Activity size={13} /> INSIGHTS / 可核对的执行
          </span>
          <h1>运行洞察</h1>
          <p>从一次请求，追溯耗时、用量与办理依据。</p>
        </div>
        <div className="insight-heading-actions">
          <button className="secondary" onClick={() => setComparison(true)}>
            策略对比
          </button>
          <button className="secondary" onClick={refresh} disabled={summary.loading}>
            <RefreshCw size={14} />
            刷新
          </button>
        </div>
      </div>
      <div className="insight-filters">
        <label>
          开始日期
          <input
            aria-label="开始日期"
            type="date"
            value={from}
            onChange={(e) => setFrom(e.target.value)}
          />
        </label>
        <label>
          结束日期
          <input
            aria-label="结束日期"
            type="date"
            value={to}
            onChange={(e) => setTo(e.target.value)}
          />
        </label>
        <label>
          状态
          <select aria-label="请求状态" value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">全部状态</option>
            {[
              'completed',
              'waiting_user',
              'failed',
              'interrupted',
              'cancelled',
              'running',
              'queued',
            ].map((v) => (
              <option key={v} value={v}>
                {statusName(v)}
              </option>
            ))}
          </select>
        </label>
        <label>
          模型
          <input
            aria-label="模型筛选"
            placeholder="精确模型标识"
            value={model}
            onChange={(e) => setModel(e.target.value)}
          />
        </label>
      </div>
      {!valid && <p className="error-note">请选择有效日期范围，最多31个自然日。</p>}
      {(summary.error || list.error) && (
        <p className="error-note" role="alert">
          {summary.error || list.error} · 可刷新重试，筛选已保留。
        </p>
      )}
      <div className="insight-kpis" aria-busy={summary.loading}>
        {[
          [
            '请求数',
            s ? number(s.request_count) : '—',
            s ? `${s.run_count} 次执行 · ${s.model_attempts} 次模型请求` : '按用户请求去重',
          ],
          [
            '完成比例',
            s?.completion_ratio == null ? '—' : `${(s.completion_ratio * 100).toFixed(1)}%`,
            '等待补充单列，不等于错误',
          ],
          [
            'P95 活跃执行',
            s ? duration(s.p95_ms) : '—',
            s
              ? `P50 ${duration(s.p50_ms)} · n=${s.timed_requests}${s.small_sample ? ' 小样本' : ''}`
              : '仅完整计时的终态请求',
          ],
          [
            '已知 Token',
            s ? number(s.input_tokens + s.output_tokens) : '—',
            s
              ? `${number(s.input_tokens)} 输入 / ${number(s.output_tokens)} 输出`
              : '以实际 usage 为准',
          ],
          [
            '费用估算',
            s ? fee(s.cost) : '—',
            s ? `${s.cost.unknown_calls} 次调用待核定 · 非实付账单` : '按匹配价格快照计量',
          ],
        ].map(([title, value, note]) => (
          <div className="insight-kpi" key={title}>
            <span>{title}</span>
            <strong>{value}</strong>
            <small>{note}</small>
          </div>
        ))}
      </div>
      {s && (
        <details className="insight-coverage">
          <summary>
            采集完整度 · usage {s.coverage.usage_known}/{s.coverage.dispatched} · 耗时{' '}
            {s.coverage.timed_calls} 次 · {s.coverage.dispatch_unknown} 次发送状态未知
          </summary>
          <p className="small muted">
            已记录版本 {s.coverage.versioned_runs}/{s.coverage.runs} 次执行；未闭合{' '}
            {s.coverage.open_spans ?? '未知'} 个节点，其中终态执行遗留{' '}
            {s.coverage.abandoned_spans ?? '未知'} 个。数据截至 {dateTime(s.measured_at)}
            ；实时状态可变化。未评测的线上请求不计算正确率。
          </p>
          <p className="small muted">
            {Object.entries(s.states)
              .map(([k, v]) => `${statusName(k)} ${v}`)
              .join(' · ') || '暂无请求'}
          </p>
        </details>
      )}
      <div className={`insight-workspace ${selected ? 'has-selection' : ''}`}>
        <aside className="insight-history" aria-label="历史请求">
          <div className="insight-section-title">
            <h2>历史请求</h2>
            <span>当前企业范围 · 每页 5 条</span>
          </div>
          <div className="insight-history-list">
            {list.loading && !list.data && <p className="muted">正在读取历史计量…</p>}
            {list.data?.requests.length === 0 && (
              <div className="insight-empty">
                <h3>这个范围暂无请求</h3>
                <p>可调整日期或状态。没有记录不代表消耗为零。</p>
              </div>
            )}
            {list.data?.requests.map((r) => (
              <button
                className={`insight-history-row ${selected === r.turn_id ? 'selected' : ''}`}
                key={r.turn_id}
                onClick={() => setSelected(r.turn_id)}
              >
                <span className="insight-row-date">
                  {dateTime(r.created_at)}
                  <span>{statusName(r.status)}</span>
                </span>
                <strong>{r.text}</strong>
                <span className="insight-row-metrics">
                  {duration(r.active_ms)} ·{' '}
                  {r.usage_known_calls
                    ? `${number(r.input_tokens + r.output_tokens)} Token`
                    : 'Token 未采集'}
                  <ChevronRight size={13} />
                </span>
              </button>
            ))}
          </div>
          <div className="insight-pagination">
            <button
              className="secondary"
              disabled={!previous.length || list.loading}
              onClick={() => {
                setCursor(previous.at(-1) ?? '');
                setPrevious((v) => v.slice(0, -1));
                setSelected('');
              }}
            >
              上一页
            </button>
            <span className="small muted">第 {previous.length + 1} 页</span>
            <button
              className="secondary"
              disabled={!list.data?.has_more || list.loading}
              onClick={() => {
                setPrevious((v) => [...v, cursor]);
                setCursor(list.data?.next_cursor ?? '');
                setSelected('');
              }}
            >
              下一页
            </button>
          </div>
        </aside>
        <div
          className="insight-trace-pane"
          ref={tracePane}
          tabIndex={0}
          aria-label="请求详情，可独立滚动"
        >
          {selected ? (
            <RequestInsight key={selected} turnId={selected} onBack={() => setSelected('')} />
          ) : (
            <div className="insight-empty insight-select-hint">
              <Activity size={30} strokeWidth={1} />
              <h2>沿着轨迹，找到依据</h2>
              <p>选择一条历史请求，查看模型、工具、失败与恢复。</p>
              <small>所有查看均只读，不重放保存或额外调用模型。</small>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
