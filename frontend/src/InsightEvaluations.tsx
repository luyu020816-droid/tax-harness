import { useState } from 'react';
import { ArrowLeft } from 'lucide-react';
import { dateTime } from './api';
import { duration } from './insight-format';
import { useInsight } from './useInsight';
type Evaluation = { evaluation_id: string; title: string; imported_at: string };
type EvaluationReport = {
  title: string;
  report: {
    model: string;
    sample_type: string;
    human_review: string;
    backend_sha256: string | null;
    dataset_sha256: string | null;
    price: { as_of: string; currency: string };
    aggregate: Record<
      string,
      {
        cases: number;
        correct: number;
        p50_ms: number;
        p95_ms: number;
        model_attempts: number;
        estimated_cost_usd: string | null;
        missing_usage_calls: number;
      }
    >;
    rows: {
      case_id: string;
      profile: string;
      repetition: number;
      correct: boolean;
      preparation_error: string | null;
    }[];
  };
};
export function Evaluations({ onBack }: { onBack: () => void }) {
  const list = useInsight<Evaluation[]>('/insights/evaluations');
  const [id, setId] = useState('');
  const selected = useInsight<EvaluationReport>(id ? `/insights/evaluations/${id}` : null);
  return (
    <section className="insight-comparison">
      <button className="text-button" onClick={onBack}>
        <ArrowLeft size={14} />
        返回运行洞察
      </button>
      <h2>策略对比</h2>
      <p className="muted">
        固定开发集、实际调用和价格快照。这里查看已导入报告，不触发实验或收费调用。
      </p>
      {list.error && <p className="error-note">{list.error}</p>}
      {list.loading && <p className="muted">读取评测记录…</p>}
      {list.data?.length === 0 && (
        <p className="empty-panel">暂无已导入报告，维护者可使用本地导入命令添加。</p>
      )}
      <div className="insight-evaluation-list">
        {list.data?.map((row) => (
          <button
            className="secondary"
            key={row.evaluation_id}
            onClick={() => setId(row.evaluation_id)}
          >
            {row.title} · {dateTime(row.imported_at)}
          </button>
        ))}
      </div>
      {selected.error && <p className="error-note">{selected.error}</p>}
      {selected.data && (
        <>
          <p className="notice">
            开发集结果 · 人工签收 {selected.data.report.human_review} · 不代表线上或未见数据正确率
          </p>
          <p className="small">
            模型 {selected.data.report.model} · 价格快照 {selected.data.report.price.as_of}
          </p>
          <div className="insight-table-scroll">
            <table>
              <thead>
                <tr>
                  <th>策略</th>
                  <th>正确 / 样本</th>
                  <th>P50 / P95</th>
                  <th>模型请求</th>
                  <th>费用估算 {selected.data.report.price.currency}</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(selected.data.report.aggregate).map(([profile, v]) => (
                  <tr key={profile}>
                    <td>{profile}</td>
                    <td>
                      {v.correct} / {v.cases}
                    </td>
                    <td>
                      {duration(v.p50_ms)} / {duration(v.p95_ms)}
                    </td>
                    <td>{v.model_attempts}</td>
                    <td>{v.estimated_cost_usd ?? `未知（${v.missing_usage_calls}次）`}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <details className="insight-evidence">
            <summary>逐样本结果与失败</summary>
            {selected.data.report.rows.map((r, i) => (
              <p key={i} className="small">
                {r.case_id} · {r.profile} · 第{r.repetition}轮 ·{' '}
                {r.correct ? '自动断言通过' : `未通过 ${r.preparation_error ?? ''}`}
              </p>
            ))}
          </details>
          <details className="insight-evidence">
            <summary>可比性与版本</summary>
            <pre>
              {JSON.stringify(
                {
                  backend: selected.data.report.backend_sha256,
                  dataset: selected.data.report.dataset_sha256,
                },
                null,
                2
              )}
            </pre>
            <p className="muted">
              不同模型、数据或配置的报告仅并列查看，不自动给出因果或胜负结论。
            </p>
          </details>
        </>
      )}
    </section>
  );
}
