import { useEffect, useState } from 'react';
import { api } from './api';
import { useResource } from './useResource';

type WorkbookPage = {
  document_id: string;
  version_id: string;
  original_name: string;
  edition: number;
  total: string;
  count: number;
  headers: { amount: string };
  query: {
    sheet: string;
    columns: { date: string; category: string; amount: string };
    date_from?: string;
    date_to_exclusive?: string;
    category?: string;
  };
  rows: [number, string, string | null, string][];
  groups: { category: string; count: number; total: string }[];
  next_offset: number | null;
  next_group_offset: number | null;
};

export function WorkbookResult({ id }: { id: string }) {
  const [page, setPage] = useState<WorkbookPage | null>(null);
  const [error, setError] = useState('');
  const [groupOffset, setGroupOffset] = useState(0);
  const [showSources, setShowSources] = useState(false);
  useEffect(() => {
    const request = new AbortController();
    setError('');
    setPage(null);
    api<WorkbookPage>(`/knowledge/workbook-results/${id}?group_offset=${groupOffset}`, {
      signal: request.signal,
    })
      .then((payload) => {
        if (!request.signal.aborted) setPage(payload);
      })
      .catch((err: Error) => {
        if (!request.signal.aborted) setError(err.message);
      });
    return () => request.abort();
  }, [id, groupOffset]);
  if (error)
    return (
      <p className="error-note small" role="status">
        {error}
      </p>
    );
  if (!page) return <p className="muted small">正在核对表格结果…</p>;
  const { query } = page;
  return (
    <section className="result-card" aria-label="Excel 计算结果">
      <strong>
        合计 {page.total} 元 · {page.count} 行
      </strong>
      <p>
        {query.date_from && query.date_to_exclusive
          ? `${query.date_from} 至 ${query.date_to_exclusive}（不含结束日）`
          : '全部日期'}
        {query.category ? ` · ${query.category}` : ''}
      </p>
      {page.groups.length > 0 && (
        <div>
          <ul>
            {page.groups.map((group) => (
              <li key={group.category}>
                {group.category} · {group.total} 元 · {group.count} 行
              </li>
            ))}
          </ul>
          <Pager
            offset={groupOffset}
            next={page.next_group_offset}
            onPage={setGroupOffset}
            label="分项"
          />
        </div>
      )}
      <button
        className="text-button"
        type="button"
        aria-expanded={showSources}
        onClick={() => setShowSources((visible) => !visible)}
      >
        {showSources ? '收起原表依据' : '查看原表依据'}
      </button>
      {showSources && <WorkbookSources key={id} id={id} />}
    </section>
  );
}

function WorkbookSources({ id }: { id: string }) {
  const [offset, setOffset] = useState(0);
  const {
    data: page,
    error,
    loading,
  } = useResource<WorkbookPage>(`/knowledge/workbook-results/${id}?offset=${offset}`);
  if (loading) return <p className="muted small">正在核对原表依据…</p>;
  if (!page || error)
    return (
      <p className="error-note small" role="status">
        引用当前不可用：{error}
      </p>
    );
  const { query } = page;
  return (
    <div>
      <p>
        {page.original_name} · v{page.edition} · {query.sheet} · {page.headers.amount}（
        {query.columns.amount} 列）
      </p>
      <ul>
        {page.rows.map(([number, date, category, amount]) => (
          <li key={number}>
            第 {number} 行（{query.columns.amount}
            {number}）· {date} · {category ?? '类别空白'} · {amount} 元
          </li>
        ))}
      </ul>
      <Pager offset={offset} next={page.next_offset} onPage={setOffset} label="来源行" />
      <a
        href={`/api/v1/knowledge/documents/${page.document_id}/file?version_id=${page.version_id}`}
      >
        下载当前有效原表
      </a>
    </div>
  );
}

function Pager({
  offset,
  next,
  onPage,
  label,
}: {
  offset: number;
  next: number | null;
  onPage: (offset: number) => void;
  label: string;
}) {
  return (
    <>
      {offset > 0 && (
        <button className="text-button" onClick={() => onPage(Math.max(0, offset - 25))}>
          上一页{label}
        </button>
      )}
      {next !== null && (
        <button className="text-button" onClick={() => onPage(next)}>
          下一页{label}
        </button>
      )}
    </>
  );
}
