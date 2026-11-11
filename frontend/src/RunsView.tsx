import { useState } from 'react';
import { importStatus } from './ImportReceipt';
import { ArrowUpRight } from 'lucide-react';
import { dateTime } from './api';
import { useResource } from './useResource';
import type { Detail, ImportSummary } from './types';
import { InsightsView } from './InsightsView';

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
  return (
    <section className={tab === 'imports' ? 'management-page' : 'runs-layout'}>
      {tab === 'imports' && (
        <div className="page-heading">
          <div>
            <h1>数据导入记录</h1>
            <p>核对本轮用了什么信息，执行了哪些操作。</p>
          </div>
        </div>
      )}
      {showImports && (
        <div className="tabs page-tabs">
          <button className={tab === 'runs' ? 'selected' : ''} onClick={() => setTab('runs')}>
            运行洞察
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
        <InsightsView company={company} />
      )}
    </section>
  );
}
