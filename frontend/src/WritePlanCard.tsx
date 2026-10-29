import { useEffect, useState } from 'react';
import { CircleAlert, ShieldCheck } from 'lucide-react';
import { api, body, companyName, dateTime, requestId } from './api';
import type { InvoiceWritePlan } from './types';

export function WritePlanCard({
  plan,
  canConfirm,
  onConfirm,
  onChanged,
}: {
  plan: InvoiceWritePlan;
  canConfirm: boolean;
  onConfirm: (plan: InvoiceWritePlan) => Promise<void>;
  onChanged: () => Promise<void>;
}) {
  const [current, setCurrent] = useState(plan);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const controller = new AbortController();
    const refresh = () => {
      setNow(Date.now());
      api<InvoiceWritePlan>(`/write-plans/${plan.plan_id}`, { signal: controller.signal })
        .then((value) => {
          if (!controller.signal.aborted) setCurrent(value);
        })
        .catch((err) => {
          if (!controller.signal.aborted) setError(err.message);
        });
    };
    refresh();
    const timer = setInterval(refresh, 4000);
    return () => {
      controller.abort();
      clearInterval(timer);
    };
  }, [plan.plan_id]);
  const expired = new Date(current.expires_at).getTime() <= now;
  const waiting = current.status === 'needs_confirmation' && !expired;
  const resumable = Boolean(current.confirmation_turn_id);
  const label =
    current.status === 'consumed'
      ? '该修改已执行，请核对操作回执'
      : current.status === 'revoked'
        ? '修改已撤销'
        : resumable
          ? '确认已受理，可查看办理结果'
          : expired
            ? '计划已过期，请发送新消息重新核对'
            : waiting
              ? '请核对后确认保存'
              : '当前计划不可确认';

  async function act(confirm: boolean) {
    setBusy(true);
    setError('');
    try {
      if (confirm) await onConfirm(current);
      else {
        setCurrent(
          await api<InvoiceWritePlan>(`/write-plans/${current.plan_id}/cancel`, {
            method: 'POST',
            body: body({ request_id: requestId() }),
          })
        );
        await onChanged();
      }
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="result-card write-plan-card">
      <div className="result-range">
        <span>{companyName(current.company_id)}</span>
        <strong>{current.invoice_id}</strong>
      </div>
      <div className="receipt-heading">
        <ShieldCheck size={20} />
        <h3>{label}</h3>
      </div>
      <p className="muted small">只保存以下字段；有效至 {dateTime(current.expires_at)}。</p>
      <dl className="saved-values">
        {Object.entries(current.patch).map(([key, value]) => (
          <div key={key}>
            <dt>{key === 'purpose' ? '用途' : '备注'}</dt>
            <dd>
              <span className="muted">{current.before_values[key]?.trim() || '空白'}</span>
              <span aria-label="修改为"> → </span>
              <strong>{value?.trim() || '清空'}</strong>
            </dd>
          </div>
        ))}
      </dl>
      {error && (
        <p className="error-note" role="alert">
          <CircleAlert size={14} /> {error}
        </p>
      )}
      {(waiting || resumable) && (
        <div className="write-plan-actions">
          <button
            className="text-button"
            disabled={!canConfirm || busy}
            onClick={() => void act(true)}
          >
            {busy ? '正在核对…' : resumable ? '查看办理结果' : '确认并保存'}
          </button>
          {waiting && (
            <button
              className="text-button"
              disabled={!canConfirm || busy}
              onClick={() => void act(false)}
            >
              撤销计划
            </button>
          )}
        </div>
      )}
    </section>
  );
}
