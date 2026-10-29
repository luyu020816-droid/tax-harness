import { useState } from 'react';
import { Download, FileText, ShieldCheck } from 'lucide-react';
import { companyName, dateTime, statusName, taskName } from './api';
import { InvoiceImportDialog } from './InvoiceImportDialog';
import { Modal } from './Modal';
import { useResource } from './useResource';
import type { Detail, RunDetail, Source, UploadedInvoice, ImportBatch } from './types';

function RunContent({ id }: { id: string }) {
  const { data, error, loading } = useResource<RunDetail>('/runs/' + id);
  const [tab, setTab] = useState('overview');
  if (loading) return <p className="muted">正在读取真实运行记录…</p>;
  if (error || !data) return <p className="error-note">{error}</p>;
  return (
    <>
      <div className="detail-kicker">
        <ShieldCheck size={16} />
        {statusName(data.run.state)}
        <a className="text-button" href={`/api/v1/runs/${id}/export`} download>
          <Download size={14} />
          导出
        </a>
      </div>
      <div className="tabs">
        {[
          ['overview', '概览'],
          ['input', '模型输入'],
          ['tools', '工具与结果'],
        ].map(([key, label]) => (
          <button key={key} className={tab === key ? 'selected' : ''} onClick={() => setTab(key)}>
            {label}
          </button>
        ))}
      </div>
      {tab === 'overview' && (
        <dl className="detail-list">
          <div>
            <dt>开始时间</dt>
            <dd>{dateTime(data.run.started_at)}</dd>
          </div>
          <div>
            <dt>模型调用</dt>
            <dd>
              {data.contexts.filter((item) => item.manifest.kind !== 'control_execution').length} 次
            </dd>
          </div>
          <div>
            <dt>工具记录</dt>
            <dd>{data.tools.length} 条</dd>
          </div>
          <div>
            <dt>输入计数</dt>
            <dd>按每轮记录标注估算或实测</dd>
          </div>
        </dl>
      )}
      {tab === 'input' &&
        data.contexts.map((item) => (
          <details className="trace-block" key={item.context_id}>
            <summary>
              {item.manifest.kind === 'control_execution'
                ? '本人确认执行快照（未调用模型）'
                : `第 ${item.model_step} 轮`}{' '}
              · {item.token_count.toLocaleString()} 输入估算单位
            </summary>
            <p className="muted small">
              计数方式：{item.count_kind}；用时 {item.duration_ms ?? '未知'} ms
            </p>
            <h4>{item.manifest.kind === 'control_execution' ? '宿主执行依据' : '实际请求'}</h4>
            <pre>{JSON.stringify(item.payload, null, 2)}</pre>
            <h4>输入与来源清单</h4>
            <pre>{JSON.stringify(item.manifest, null, 2)}</pre>
          </details>
        ))}
      {tab === 'tools' &&
        data.tools.map((item) => (
          <details className="trace-block" key={item.tool_call_id}>
            <summary>
              {item.name} · {item.state === 'done' ? '已执行' : item.state}
            </summary>
            <h4>请求参数</h4>
            <pre>{JSON.stringify(item.arguments, null, 2)}</pre>
            <h4>实际结果</h4>
            <pre>
              {item.actual_result
                ? JSON.stringify(JSON.parse(item.actual_result), null, 2)
                : '尚无结果'}
            </pre>
          </details>
        ))}
    </>
  );
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
