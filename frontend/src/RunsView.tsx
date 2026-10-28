import { useState } from 'react';
import { importStatus } from './ImportReceipt';
import { ArrowUpRight } from 'lucide-react';
import { dateTime, statusName } from './api';
import { useResource } from './useResource';
import type { Detail, RunSummary, ImportSummary } from './types';

export function RunsView({
  company,
  onDetail,
  showImports = false,
}: {
  company: string | null;
  showImports?: boolean;
  onDetail: (value: Detail) => void;
}) {
  const [tab, setTab] = useState('runs');
  const [cursor, setCursor] = useState(0);
  const imports = useResource<{
    batches: ImportSummary[];
    has_more: boolean;
    next_cursor: number | null;
  }>(
    showImports
      ? '/invoice-imports?cursor=' + cursor + (company ? '&company_id=' + company : '')
      : null
  );
  const { data, error, loading } = useResource<RunSummary[]>(
    '/runs' + (company ? `?company_id=${company}` : '')
  );
  return (
    <section className="management-page">
      <div className="page-heading">
        <div>
          <h1>运行记录</h1>
          <p>核对本轮用了什么信息，执行了哪些操作。</p>
        </div>
      </div>
      {showImports && (
        <div className="tabs page-tabs">
          <button className={tab === 'runs' ? 'selected' : ''} onClick={() => setTab('runs')}>
            对话运行
          </button>
          <button className={tab === 'imports' ? 'selected' : ''} onClick={() => setTab('imports')}>
            数据导入
          </button>
        </div>
      )}
      {tab === 'imports' ? (
        <>
          {imports.error && <p className="error-note">{imports.error}</p>}
          {imports.loading ? (
            <p className="muted">正在读取导入记录…</p>
          ) : (
            <>
              <div className="run-list">
                {imports.data?.batches.map((batch) => (
                  <button
                    className="run-row"
                    key={batch.batch_id}
                    onClick={() => onDetail({ kind: 'import', id: batch.batch_id })}
                  >
                    <div>
                      <strong>{batch.original_name}</strong>
                      <p>{dateTime(batch.created_at)}</p>
                    </div>
                    <span className="badge quiet">{importStatus(batch.status)}</span>
                    <ArrowUpRight size={16} />
                  </button>
                ))}
              </div>
              {!imports.data?.batches.length && <p className="empty-panel">还没有数据导入记录。</p>}
              <div className="form-actions">
                <button
                  className="secondary"
                  disabled={!cursor}
                  onClick={() => setCursor(Math.max(0, cursor - 20))}
                >
                  上一页
                </button>
                <button
                  className="secondary"
                  disabled={!imports.data?.has_more}
                  onClick={() => setCursor(imports.data?.next_cursor ?? 0)}
                >
                  下一页
                </button>
              </div>
            </>
          )}
        </>
      ) : (
        <>
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
              <h3>还没有办理记录</h3>
              <p>开始对话后，实际输入、工具调用和结果会保存在这里。</p>
            </div>
          )}
        </>
      )}
    </section>
  );
}
