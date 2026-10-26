import { ArrowUpRight, History } from 'lucide-react';
import { dateTime, statusName } from './api';
import { useResource } from './useResource';
import type { Detail, RunSummary } from './types';

export function RunsView({
  company,
  onDetail,
}: {
  company: string | null;
  onDetail: (value: Detail) => void;
}) {
  const { data, error, loading } = useResource<RunSummary[]>(
    '/runs' + (company ? `?company_id=${company}` : '')
  );
  return (
    <section className="management-page">
      <div className="page-heading">
        <div>
          <span className="eyebrow">有据可查</span>
          <h1>运行记录</h1>
          <p>核对本轮用了什么信息，执行了哪些操作。</p>
        </div>
      </div>
      {error && <p className="error-note">{error}</p>}
      {loading ? (
        <p className="muted">正在读取记录…</p>
      ) : data?.length ? (
        <div className="run-list">
          {data.map((run) => (
            <button
              className="run-row"
              key={run.run_id}
              onClick={() => onDetail({ kind: 'run', id: run.run_id })}
            >
              <div className="memory-icon">
                <History size={18} />
              </div>
              <div>
                <strong>{run.text}</strong>
                <p>{dateTime(run.started_at)}</p>
              </div>
              <span className={`badge ${run.state === 'completed' ? 'success' : 'quiet'}`}>
                {statusName(run.state)}
              </span>
              <ArrowUpRight size={16} />
            </button>
          ))}
        </div>
      ) : (
        <div className="empty-panel">
          <History size={32} />
          <h3>还没有办理记录</h3>
          <p>开始对话后，实际输入、工具调用和结果会保存在这里。</p>
        </div>
      )}
    </section>
  );
}
