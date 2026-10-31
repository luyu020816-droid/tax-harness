import { companyName } from './api';
import { locatorLabel, useKnowledgeEvidence } from './KnowledgeCitation';
import { stageName } from './KnowledgeView';
import { useResource } from './useResource';
import type { KnowledgeDocument } from './types';

export function KnowledgeDocumentDetails({ id }: { id: string }) {
  const { data, error, loading } = useResource<KnowledgeDocument>(`/knowledge/documents/${id}`);
  if (loading) return <p className="muted">正在读取资料状态…</p>;
  if (!data) return <p className="error-note">{error}</p>;
  return (
    <>
      <h3>{data.title}</h3>
      <p className="muted">
        {companyName(data.company_id)} · {data.scope === 'company' ? '企业共享' : '仅本人'} ·{' '}
        {data.status === 'active' ? '可用' : '已停用'}
      </p>
      <div className="memory-list">
        {data.versions?.map((version) => (
          <article key={version.version_id} className="memory-row">
            <div className="memory-content">
              <strong>
                第{version.edition}版
                {version.version_id === data.active_version_id ? ' · 当前版本' : ''}
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
            <a
              className="text-button"
              href={`/api/v1/knowledge/documents/${id}/file?version_id=${version.version_id}`}
              download
            >
              下载原文件
            </a>
          </article>
        ))}
      </div>
    </>
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
      <blockquote className="knowledge-excerpt">{data.excerpt}</blockquote>
      {data.locator.some((item) => item.extraction === 'ocr') && (
        <p className="muted small">扫描件识别文字，请对照原PDF核对。</p>
      )}
      <a
        className="text-button"
        href={`/api/v1/knowledge/documents/${data.document_id}/file?version_id=${data.version_id}`}
        download
      >
        下载引用版本原文件
      </a>
      <p className="notice">以上为资料原文，不代表已执行任何业务操作。</p>
    </>
  );
}
