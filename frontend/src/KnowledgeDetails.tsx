import { companyName } from './api';
import {
  locatorLabel,
  useKnowledgeEvidence,
  versionFilePath,
  versionTimingLabel,
} from './KnowledgeCitation';
import { stageName } from './KnowledgeView';
import { useResource } from './useResource';
import type { KnowledgeDocument } from './types';

export function KnowledgeDocumentDetails({ id }: { id: string }) {
  const { data, error, loading } = useResource<KnowledgeDocument>(`/knowledge/documents/${id}`);
  if (loading) return <p className="muted">正在读取资料状态…</p>;
  if (!data) return <p className="error-note">{error}</p>;
  const workbook = data.versions?.find(
    (version) =>
      version.version_id === data.active_version_id && /\.xlsx$/i.test(version.original_name)
  );
  return (
    <>
      <h3>{data.title}</h3>
      <p className="muted">
        {companyName(data.company_id)} · {data.scope === 'company' ? '企业共享' : '仅本人'} ·{' '}
        {data.status !== 'active' ? '已停用' : data.active_version_id ? '可用' : '当前无适用版本'}
      </p>
      {workbook && data.status === 'active' && (
        <WorkbookStructure
          id={id}
          version={workbook.version_id}
          asOf={data.version_timing?.as_of}
        />
      )}
      <div className="memory-list">
        {data.versions?.map((version) => (
          <article key={version.version_id} className="memory-row">
            <div className="memory-content">
              <strong>
                第{version.edition}版
                {version.version_id === data.active_version_id ? ' · 当前版本' : ''}
                {version.published && version.version_id !== data.active_version_id
                  ? ' · 已发布'
                  : ''}
              </strong>
              <p className="muted small">
                {version.original_name} · {stageName(version.state)}
              </p>
              {version.effective_from !== '0001-01-01' && (
                <p className="muted small">
                  生效区间：{version.effective_from} 至 {version.effective_to ?? '未设结束日期'}
                </p>
              )}
              {version.error && <p className="error-note">{version.error.message}</p>}
            </div>
            {version.version_id === data.active_version_id && data.status === 'active' && (
              <a
                className="text-button"
                href={versionFilePath(id, version.version_id, data.version_timing?.as_of)}
                download
              >
                下载原文件
              </a>
            )}
          </article>
        ))}
      </div>
    </>
  );
}

function WorkbookStructure({
  id,
  version,
  asOf,
}: {
  id: string;
  version: string;
  asOf?: string | null;
}) {
  const search = new URLSearchParams({ version_id: version });
  if (asOf) search.set('as_of', asOf);
  const { data, error } = useResource<{
    sheets: {
      name: string;
      header_row: number | null;
      reason: string | null;
      columns: Record<string, Record<string, string>>;
    }[];
  }>(`/knowledge/documents/${id}/workbook?${search}`);
  if (error) return <p className="error-note">{error}</p>;
  if (!data) return null;
  return (
    <details>
      <summary>查看工作表和识别列</summary>
      <ul>
        {data.sheets.map((sheet) => (
          <li key={sheet.name}>
            <strong>{sheet.name}</strong>
            {sheet.reason
              ? ` · ${sheet.reason}`
              : ` · 表头第 ${sheet.header_row} 行 · ${Object.values(sheet.columns)
                  .flatMap((columns) =>
                    Object.entries(columns).map(([col, name]) => `${col} 列 ${name}`)
                  )
                  .join('、')}`}
          </li>
        ))}
      </ul>
    </details>
  );
}

export function EvidenceDetails({ id }: { id: string }) {
  const { evidence: data, error } = useKnowledgeEvidence(id);
  if (!data && !error) return <p className="muted">正在重新核验引用…</p>;
  if (!data) return <p className="error-note">引用当前不可用：{error}</p>;
  return (
    <>
      <h3>{data.title}</h3>
      <p className="muted">
        {companyName(data.company_id)} · 第{data.edition}版 · {locatorLabel(data.locator)}
      </p>
      {data.version_timing && (
        <p className="muted small">{versionTimingLabel(data.version_timing)}</p>
      )}
      <blockquote className="knowledge-excerpt">{data.excerpt}</blockquote>
      {data.locator.some((item) => item.extraction === 'ocr') && (
        <p className="muted small">扫描件识别文字，请对照原PDF核对。</p>
      )}
      <a
        className="text-button"
        href={versionFilePath(
          data.document_id,
          data.version_id,
          data.version_timing?.as_of,
          data.period
        )}
        download
      >
        下载引用版本原文件
      </a>
      <p className="notice">以上为资料原文，不代表已执行任何业务操作。</p>
    </>
  );
}
