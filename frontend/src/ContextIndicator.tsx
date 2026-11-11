import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { api, body } from './api';
import './context-indicator.css';

interface Preview {
  input_estimate: number;
  input_limit: number;
  output_reserve: number;
  margin: number | null;
  effective_window: number | null;
  pressure_ratio: number | null;
  window_status: string;
  count_kind: string;
}

export function ContextIndicator({
  sessionId,
  draft,
  version,
}: {
  sessionId: string | null;
  draft: string;
  version: string;
}) {
  const [preview, setPreview] = useState<Preview | null>(null);
  const [error, setError] = useState('');
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState({ left: 0, top: 0 });
  const trigger = useRef<HTMLButtonElement>(null);
  const popover = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    if (!open) return;
    const place = () => {
      const anchor = trigger.current?.getBoundingClientRect();
      const panel = popover.current?.getBoundingClientRect();
      if (!anchor || !panel) return;
      setPosition({
        left: Math.max(
          12,
          Math.min(anchor.right - panel.width, window.innerWidth - panel.width - 12)
        ),
        top: Math.max(
          12,
          Math.min(anchor.top - panel.height - 10, window.innerHeight - panel.height - 12)
        ),
      });
    };
    place();
    const dismiss = (event: PointerEvent) => {
      if (
        !trigger.current?.contains(event.target as Node) &&
        !popover.current?.contains(event.target as Node)
      )
        setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setOpen(false);
        trigger.current?.focus();
      }
    };
    window.addEventListener('resize', place);
    window.addEventListener('scroll', place, true);
    document.addEventListener('pointerdown', dismiss);
    document.addEventListener('keydown', escape);
    return () => {
      window.removeEventListener('resize', place);
      window.removeEventListener('scroll', place, true);
      document.removeEventListener('pointerdown', dismiss);
      document.removeEventListener('keydown', escape);
    };
  }, [open, preview, error]);
  useEffect(() => {
    setPreview(null);
    if (!sessionId || draft.length > 16000) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      api<Preview>(`/sessions/${sessionId}/context-preview`, {
        method: 'POST',
        body: body({ draft }),
        signal: controller.signal,
      })
        .then((result) => {
          if (!controller.signal.aborted) {
            setPreview(result);
            setError('');
          }
        })
        .catch((failure) => {
          if (!controller.signal.aborted) setError(failure.message || '估算暂不可用');
        });
    }, 450);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [sessionId, draft, version]);
  const ratio = preview?.pressure_ratio;
  const percent = ratio == null ? null : Math.round(ratio * 100);
  const ring = Math.min(100, Math.max(0, percent ?? 0));
  const label = percent == null ? '上下文 --' : `上下文 ${percent}%`;
  return (
    <div className="context-indicator">
      <button
        type="button"
        ref={trigger}
        className="context-indicator-trigger"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
        aria-label={`${label}，点击查看估算详情`}
      >
        <span
          aria-hidden="true"
          className="context-ring"
          style={{
            background: `conic-gradient(${ring >= 90 ? '#cf7a35' : '#5874cc'} ${ring}%, #dfe5ee 0)`,
          }}
        >
          <span>{percent == null ? '--' : `${percent}%`}</span>
        </span>
        <span>上下文</span>
      </button>
      {open &&
        createPortal(
          <div ref={popover} className="context-indicator-popover" role="status" style={position}>
            {preview?.effective_window ? (
              <>
                <strong>下一次输入估算</strong>
                <span>输入约 {preview.input_estimate.toLocaleString()} tokens</span>
                <span>
                  输出预留 {preview.output_reserve.toLocaleString()} · 余量{' '}
                  {preview.margin?.toLocaleString()}
                </span>
                <span>
                  有效窗口 {preview.effective_window.toLocaleString()} · 输入上限{' '}
                  {preview.input_limit.toLocaleString()}
                </span>
                <small>包含草稿；范围引用、附件及后续工具结果可能变化，实际发送时重新计算。</small>
              </>
            ) : (
              <span>{error || (preview ? '模型容量未确认' : '等待上下文估算')}</span>
            )}
          </div>,
          document.body
        )}
    </div>
  );
}
