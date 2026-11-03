import { RunInsightDrawer } from './InsightTrace';
import { FileText } from 'lucide-react';
import { companyName, dateTime, statusName, taskName } from './api';
import { InvoiceImportDialog } from './InvoiceImportDialog';
import { EvidenceDetails, KnowledgeDocumentDetails } from './KnowledgeDetails';
import { Modal } from './Modal';
import { useResource } from './useResource';
import type { Detail, Source, UploadedInvoice, ImportBatch } from './types';

function RunContent({ id }: { id: string }) {
  return <RunInsightDrawer runId={id} />;
}

function SourceContent({ id }: { id: string }) {
  const { data, error, loading } = useResource<Source>('/sources/' + id);
  if (loading) return <p className="muted">正在读取来源…</p>;
  if (!data) return <p className="error-note">{error}</p>;
  return (
    <>
      <p className="notice">历史原文用于核对，当前有效状态以任务和偏好列表为准。</p>
      <p className="muted small">
        {dateTime(data.created_at)} · {data.role === 'user' ? '用户输入' : '实际处理结果'}
        {data.recall_state !== 'active' ? ' · 已退出普通召回' : ''}
      </p>
      <pre className="source-text">{data.content}</pre>
    </>
  );
}

export function Details({
  detail,
  onClose,
  company,
  sessionId,
  onUploaded,
  onImportChanged,
}: {
  detail: Detail;
  onClose: () => void;
  company?: string | null;
  sessionId?: string | null;
  onUploaded?: (upload: UploadedInvoice) => void;
  onImportChanged?: (batch: ImportBatch) => void;
}) {
  if (detail.kind === 'import')
    return (
      <InvoiceImportDialog
        company={company ?? null}
        sessionId={sessionId ?? null}
        initialBatchId={detail.id}
        onClose={onClose}
        onUploaded={onUploaded}
        onChanged={onImportChanged}
      />
    );
  if (detail.kind === 'knowledge')
    return (
      <Modal title="资料版本与原文件" onClose={onClose} drawer>
        <KnowledgeDocumentDetails id={detail.id} />
      </Modal>
    );
  if (detail.kind === 'evidence')
    return (
      <Modal title="引用原文" onClose={onClose} drawer>
        <EvidenceDetails id={detail.id} />
      </Modal>
    );
  const title = { rule: '规则依据', run: '办理记录', task: '任务详情', source: '历史来源原文' }[
    detail.kind
  ];
  return (
    <Modal title={title} onClose={onClose} drawer>
      {detail.kind === 'rule' ? (
        <>
          <div className="detail-kicker">
            <FileText size={16} />
            {detail.data.rule_code} · {detail.data.business_version} 版
          </div>
          <h3 className="rule-title">{detail.data.display_name}</h3>
          <p className="muted small">
            生效日期 {detail.data.effective_from} ·{' '}
            {detail.data.status === 'active' ? '启用' : '已停用'}
          </p>
          <div className="rule-body">{detail.data.full_text}</div>
        </>
      ) : detail.kind === 'run' ? (
        <RunContent id={detail.id} />
      ) : detail.kind === 'source' ? (
        <SourceContent id={detail.id} />
      ) : (
        <>
          <div className="detail-kicker">
            {companyName(detail.data.company_id)} · {detail.data.period ?? '月份待确认'}
          </div>
          <h3>{taskName(detail.data.intent)}</h3>
          <dl className="detail-list">
            <div>
              <dt>状态</dt>
              <dd>{statusName(detail.data.status)}</dd>
            </div>
            <div>
              <dt>最近更新</dt>
              <dd>{dateTime(detail.data.updated_at)}</dd>
            </div>
            <div>
              <dt>待处理</dt>
              <dd>
                {detail.data.unresolved_json.needs_refresh || detail.data.snapshot_current === false
                  ? '当前资料待重新检查'
                  : detail.data.unresolved_json.missing_count !== undefined
                    ? `${detail.data.unresolved_json.missing_count} 张待补（最近一次检查）`
                    : (detail.data.unresolved_json.question ?? '等待继续办理')}
              </dd>
            </div>
          </dl>
        </>
      )}
    </Modal>
  );
}
