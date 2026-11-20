import { CheckCheck, CircleAlert } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { api, companyName, dateTime, isUncertainRequest } from './api';
import type { OperationCard } from './types';

export function InvoiceReceipt({
  card,
  checked = false,
  children,
}: {
  card: OperationCard;
  checked?: boolean;
  children?: ReactNode;
}) {
  const verified = card.verification_state === 'verified';
  const committed = ['committed', 'no_change'].includes(card.commit_state);
  const label =
    card.commit_state === 'rejected'
      ? '本项未保存'
      : !committed
        ? checked
          ? '已核实，尚未提交'
          : '保存状态待核实'
        : card.verification_state === 'changed_since_commit'
          ? '本次曾提交，当前值已有变化'
          : verified
            ? card.commit_state === 'no_change'
              ? '当前已经是该值，已核验'
              : '已保存并核验'
            : '已提交，核验待完成';
  return (
    <section className={`result-card receipt-card ${verified ? 'verified' : 'unverified'}`}>
      <div className="result-range">
        <span>{companyName(card.company_id)}</span>
        {card.period && <span>{card.period.replace('-', ' 年 ')} 月</span>}
        {card.as_of && <time>{dateTime(card.as_of)} 查询</time>}
      </div>
      <div className="receipt-heading">
        {verified ? <CheckCheck size={20} /> : <CircleAlert size={20} />}
        <h3>{label}</h3>
        <span>{card.invoice_id}</span>
      </div>
      <p className="operation-number small muted">
        操作号 <code>{card.operation_id}</code>
      </p>
      <dl className="saved-values">
        {Object.entries(card.saved_values ?? card.patch).map(([key, value]) => (
          <div key={key}>
            <dt>
              {!committed ? '计划' : ''}
              {key === 'purpose' ? '用途' : '备注'}
            </dt>
            <dd>{value?.trim() ? value : committed ? '已清空' : '清空'}</dd>
          </div>
        ))}
      </dl>
      {card.error && <p className="small">{card.error.message}</p>}
      {children}
    </section>
  );
}

type Verification = {
  operation: OperationCard;
  can_resume: boolean;
  blocked_reason?: string;
};
type RecoveryResponse = (Verification & { queued?: false }) | { queued: true; turn_id: string };

export function InvoiceOperation({
  card,
  turnId,
  disabled,
  canUpdate,
  onQueued,
}: {
  card: OperationCard;
  turnId: string;
  disabled: boolean;
  canUpdate: boolean;
  onQueued: (turnId: string) => void;
}) {
  const [verification, setVerification] = useState<Verification | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const active = useRef<AbortController | null>(null);
  useEffect(() => () => active.current?.abort(), []);

  async function check(action: 'verify' | 'resume') {
    if (active.current || (action === 'resume' && (disabled || !canUpdate))) return;
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setError('');
    try {
      const result = await api<RecoveryResponse>(
        `/turns/${turnId}/operations/${card.operation_id}/${action}`,
        { method: 'POST', signal: controller.signal }
      );
      if (controller.signal.aborted) return;
      if (result.queued) {
        setVerification(null);
        onQueued(result.turn_id);
      } else {
        if (result.operation?.operation_id !== card.operation_id)
          throw new Error('未收到原操作的核实结果，请稍后重试。');
        setVerification(result);
      }
    } catch (failure) {
      if (controller.signal.aborted) return;
      setVerification(null);
      setError(
        isUncertainRequest(failure)
          ? '操作状态暂时无法核实，请稍后按原操作号再查。'
          : (failure as Error).message
      );
    } finally {
      if (!controller.signal.aborted) setBusy(false);
      active.current = null;
    }
  }

  const current = verification?.operation ?? card;
  return (
    <InvoiceReceipt card={current} checked={Boolean(verification)}>
      <div className="operation-actions">
        <button className="text-button" disabled={busy} onClick={() => void check('verify')}>
          核实结果
        </button>
        {verification?.can_resume && canUpdate && (
          <button
            className="text-button"
            disabled={busy || disabled}
            onClick={() => void check('resume')}
          >
            继续原操作
          </button>
        )}
      </div>
      {busy && (
        <p role="status" className="small muted">
          正在核对原操作…
        </p>
      )}
      {(error || verification?.blocked_reason) && (
        <p role="status" className="small">
          {error || verification?.blocked_reason}
        </p>
      )}
    </InvoiceReceipt>
  );
}
