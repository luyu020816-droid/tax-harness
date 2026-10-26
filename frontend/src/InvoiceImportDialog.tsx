import { useEffect, useRef, useState, type ChangeEvent } from 'react';
import { FileSpreadsheet, LoaderCircle, RefreshCw } from 'lucide-react';
import { api, body, companyName, money, requestId } from './api';
import { ImportReceipt } from './ImportReceipt';
import { Modal } from './Modal';
import type { ImportBatch, UploadedInvoice } from './types';

export function InvoiceImportDialog({
  company,
  sessionId,
  initialBatchId,
  onClose,
  onUploaded,
  onChanged,
}: {
  company: string | null;
  sessionId: string | null;
  initialBatchId?: string;
  onClose: () => void;
  onUploaded?: (upload: UploadedInvoice) => void;
  onChanged?: (batch: ImportBatch) => void;
}) {
  const [batch, setBatch] = useState<ImportBatch | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [skipIssues, setSkipIssues] = useState(false);
  const [offset, setOffset] = useState(0);
  const live = useRef(true);
  const confirmRequest = useRef(requestId());
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);

  function receive(next: ImportBatch) {
    setBatch(next);
    setToken(next.confirmation_token ?? null);
    setSkipIssues(next.policy === 'skip_issues');
    confirmRequest.current = requestId();
    onChanged?.(next);
  }
  useEffect(() => {
    if (!initialBatchId) return;
    const controller = new AbortController();
    setBusy(true);
    api<ImportBatch>(`/invoice-imports/${initialBatchId}`, { signal: controller.signal })
      .then((next) => {
        if (!controller.signal.aborted) setBatch(next);
      })
      .catch((err) => {
        if (!controller.signal.aborted) setError(err.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setBusy(false);
      });
    return () => controller.abort();
  }, [initialBatchId]);

  async function preview(current = batch, policy = skipIssues ? 'skip_issues' : 'strict') {
    if (!current || !sessionId) return;
    setBusy(true);
    setError('');
    setToken(null);
    try {
      const next = await api<ImportBatch>(`/invoice-imports/${current.batch_id}/preview`, {
        method: 'POST',
        body: body({
          session_id: sessionId,
          expected_revision: current.revision,
          client_request_id: requestId(),
          policy,
        }),
      });
      if (live.current) {
        setOffset(0);
        receive(next);
      }
    } catch (err) {
      if (live.current) setError((err as Error).message);
    } finally {
      if (live.current) setBusy(false);
    }
  }
  async function upload(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file || !company || !sessionId) return;
    if (!file.name.toLowerCase().endsWith('.xlsx') || file.size > 5 * 1024 * 1024) {
      setError('请选择不超过 5 MiB 的 XLSX 文件。');
      return;
    }
    setBusy(true);
    setError('');
    const data = new FormData();
    data.set('file', file);
    data.set('company_id', company);
    data.set('session_id', sessionId);
    data.set('client_request_id', requestId());
    try {
      const attachment = await api<UploadedInvoice>('/uploads/invoices', {
        method: 'POST',
        body: data,
      });
      if (!live.current) return;
      onUploaded?.(attachment);
      const created = await api<ImportBatch>('/invoice-imports', {
        method: 'POST',
        body: body({
          upload_id: attachment.upload_id,
          session_id: sessionId,
          client_request_id: requestId(),
        }),
      });
      if (!live.current) return;
      setBatch(created);
      await preview(created, 'strict');
    } catch (err) {
      if (live.current) setError((err as Error).message);
    } finally {
      if (live.current) setBusy(false);
    }
  }
  async function refresh(nextOffset = 0) {
    if (!batch) return;
    setBusy(true);
    setError('');
    try {
      const next = await api<ImportBatch>(
        `/invoice-imports/${batch.batch_id}?cursor=${nextOffset}`
      );
      if (live.current) {
        receive(next);
        setOffset(nextOffset);
      }
    } catch (err) {
      if (live.current) setError((err as Error).message);
    } finally {
      if (live.current) setBusy(false);
    }
  }
  async function confirm() {
    if (!batch || !token || !sessionId) return;
    setBusy(true);
    setError('');
    try {
      const next = await api<ImportBatch>(`/invoice-imports/${batch.batch_id}/confirm`, {
        method: 'POST',
        body: body({
          session_id: sessionId,
          expected_revision: batch.revision,
          preview_hash: batch.preview_hash,
          confirmation_token: token,
          client_request_id: confirmRequest.current,
        }),
      });
      if (live.current) receive(next);
    } catch (err) {
      if (live.current) setError((err as Error).message + ' 可先核对批次结果，勿重新上传。');
    } finally {
      if (live.current) setBusy(false);
    }
  }
  async function cancel() {
    if (!batch) return;
    setBusy(true);
    setError('');
    try {
      const next = await api<ImportBatch>(`/invoice-imports/${batch.batch_id}/cancel`, {
        method: 'POST',
        body: body({ expected_revision: batch.revision, client_request_id: requestId() }),
      });
      if (live.current) receive(next);
    } catch (err) {
      if (live.current) setError((err as Error).message);
    } finally {
      if (live.current) setBusy(false);
    }
  }
  const terminal = Boolean(batch && ['committed', 'no_change', 'cancelled'].includes(batch.status));
  const matching = Boolean(sessionId && company && (!batch || company === batch.company_id));
  const issues = (batch?.counts.conflict ?? 0) + (batch?.counts.invalid ?? 0);
  return (
    <Modal drawer title="发票数据导入" onClose={onClose}>
      <p className="notice">扩展数据集 · 仅追加新发票，已有资料和银行记录保持原样。</p>
      {!batch ? (
        <div className="import-upload">
          <FileSpreadsheet size={32} />
          <h3>{company ? `追加至${companyName(company)}` : '请先选择企业'}</h3>
          <p className="muted">
            选择模板 XLSX，最多 5 MiB、2000 条数据。系统先预览，确认后才入库。
          </p>
          <a href="/api/v1/invoice-import-template" download className="text-button">
            下载发票模板
          </a>
          <label className="upload-label">
            选择 Excel 文件
            <input
              type="file"
              accept=".xlsx"
              aria-label="选择发票 Excel"
              disabled={busy || !matching}
              onChange={upload}
            />
          </label>
        </div>
      ) : (
        <>
          <ImportReceipt batch={batch} />
          <div className="import-counts">
            {[
              ['待新增', batch.counts.new],
              ['已有重复', batch.counts.duplicate_existing],
              ['文件重复', batch.counts.duplicate_in_file],
              ['冲突', batch.counts.conflict],
              ['错误', batch.counts.invalid],
            ].map(([label, count]) => (
              <span key={label}>
                <small>{label}</small>
                <strong>{count ?? 0}</strong>
              </span>
            ))}
          </div>
          {batch.counts.new_amount && (
            <p className="muted">本次待新增金额：{money(batch.counts.new_amount)} 元</p>
          )}
          {batch.counts.error && <p className="error-note">{batch.counts.error.message}</p>}
          {batch.rows.length > 0 && (
            <div className="table-scroll import-table">
              <table>
                <thead>
                  <tr>
                    <th>原行号</th>
                    <th>发票编号</th>
                    <th>状态</th>
                    <th>说明</th>
                  </tr>
                </thead>
                <tbody>
                  {batch.rows.map((row) => (
                    <tr key={row.row_number}>
                      <td>{row.row_number}</td>
                      <td>{row.normalized_values.invoice_id ?? '—'}</td>
                      <td>
                        {
                          {
                            new: '待新增',
                            duplicate_existing: '已存在',
                            duplicate_in_file: '文件重复',
                            conflict: '冲突',
                            invalid: '错误',
                          }[row.classification]
                        }
                      </td>
                      <td>
                        {row.reason_message ??
                          (batch.status === 'committed' ? '已按确认计划入库' : '确认后追加')}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {(offset > 0 || batch.has_more) && (
            <div className="form-actions">
              <button
                className="secondary"
                disabled={busy || !offset}
                onClick={() => refresh(Math.max(0, offset - 20))}
              >
                上一页
              </button>
              <button
                className="secondary"
                disabled={busy || !batch.has_more}
                onClick={() => refresh(batch.next_cursor ?? 0)}
              >
                下一页
              </button>
            </div>
          )}
          {!terminal && (
            <>
              {!matching && (
                <p className="error-note">
                  请切换到{companyName(batch.company_id)}的会话后重新预览。
                </p>
              )}
              {issues > 0 && !batch.counts.error && (
                <label className="skip-choice">
                  <input
                    type="checkbox"
                    checked={skipIssues}
                    disabled={busy}
                    onChange={(event) => {
                      setSkipIssues(event.target.checked);
                      setToken(null);
                    }}
                  />
                  跳过错误与冲突，只追加合法新增项
                </label>
              )}
              {(batch.confirmation_expired || !token) && (
                <p className="muted small">确认前请生成当前预览；上次预览不自动授权新的差异。</p>
              )}
              <div className="form-actions">
                <button className="secondary" disabled={busy} onClick={cancel}>
                  取消批次
                </button>
                <button
                  className="secondary"
                  disabled={busy || !matching}
                  onClick={() => preview()}
                >
                  重新预览
                </button>
                <button
                  className="primary"
                  disabled={
                    busy ||
                    !matching ||
                    !token ||
                    batch.status !== 'preview_ready' ||
                    batch.confirmation_expired
                  }
                  onClick={confirm}
                >
                  向{companyName(batch.company_id)}追加 {batch.counts.new ?? 0} 张发票
                </button>
              </div>
            </>
          )}
          <p className="muted small import-batch-id">批次编号：{batch.batch_id}</p>
          <button className="text-button" disabled={busy} onClick={() => refresh(offset)}>
            <RefreshCw size={13} />
            核对导入结果
          </button>
        </>
      )}
      {busy && (
        <p className="processing" role="status">
          <LoaderCircle size={16} className="spinning" />
          正在处理，请保留本批次以便核对
        </p>
      )}
      {error && (
        <p className="error-note" role="alert">
          {error}
        </p>
      )}
    </Modal>
  );
}
