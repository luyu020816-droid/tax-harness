import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { api, body } from './api';
import type { InactiveSkill } from './types';
import './context-indicator.css';

interface Usage {
  status: 'no_calls' | 'measured' | 'unknown';
  input_tokens: number | null;
  effective_window: number | null;
  model?: string;
}
interface Preview {
  input_estimate: number;
  input_limit: number;
  effective_window: number | null;
  pressure_ratio: number | null;
  output_reserve: number;
  margin: number | null;
  history_groups: number;
  tool_count: number;
  inactive_skills?: InactiveSkill[];
  unaccounted_items: string[];
}

export function ContextIndicator({
  sessionId,
  version,
  draft = '',
  agentId = null,
}: {
  sessionId: string | null;
  version: string;
  draft?: string;
  agentId?: string | null;
}) {
  const [usage, setUsage] = useState<Usage | null>(null);
  const [error, setError] = useState('');
  const [preview, setPreview] = useState<Preview | null>(null);
  const [previewError, setPreviewError] = useState('');
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
  }, [open, usage, error, preview, previewError]);
  useEffect(() => {
    setUsage(null);
    setError('');
    if (!sessionId) return;
    const controller = new AbortController();
    api<Usage>(`/sessions/${sessionId}/context-usage`, { signal: controller.signal })
      .then((result) => {
        if (!controller.signal.aborted) setUsage(result);
      })
      .catch((failure) => {
        if (!controller.signal.aborted) setError(failure.message || '用量暂不可用');
      });
    return () => controller.abort();
  }, [sessionId, version]);
  useEffect(() => {
    setPreview(null);
    setPreviewError('');
    if (!sessionId || !open) return;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      void api<Preview>(`/sessions/${sessionId}/context-preview`, {
        method: 'POST',
        body: body({ draft, agent_id: agentId }),
        signal: controller.signal,
      })
        .then((value) => {
          if (!controller.signal.aborted) {
            if (
              typeof value.input_estimate !== 'number' ||
              typeof value.input_limit !== 'number' ||
              !Array.isArray(value.unaccounted_items)
            ) {
              setPreviewError('预估结果格式不完整，请稍后重试。');
            } else setPreview(value);
          }
        })
        .catch((failure) => {
          if (!controller.signal.aborted) setPreviewError(failure.message);
        });
    }, 300);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [sessionId, version, draft, agentId, open]);
  const ratio =
    usage?.effective_window && usage.input_tokens != null
      ? usage.input_tokens / usage.effective_window
      : null;
  const percent = ratio == null ? null : Math.round(ratio * 100);
  const ring = Math.min(100, Math.max(0, percent ?? 0));
  const label = percent == null ? '上下文用量 --' : `上下文用量 ${percent}%`;
  return (
    <div className="context-indicator">
      <button
        type="button"
        ref={trigger}
        className="context-indicator-trigger"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
        aria-label={`${label}，点击查看详情`}
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
        <span>实际输入</span>
      </button>
      {open &&
        createPortal(
          <div
            ref={popover}
            className="context-indicator-popover"
            role="status"
            aria-label="上下文详情"
            style={position}
          >
            {previewError && <span>下次输入预估不可用：{previewError}</span>}
            {preview && (
              <>
                <strong>下一次输入预估（含当前草稿）</strong>
                <span>
                  约 {preview.input_estimate.toLocaleString()} Token，输入上限{' '}
                  {preview.input_limit.toLocaleString()}
                </span>
                <span>
                  历史 {preview.history_groups} 组 · 工具 {preview.tool_count} 项
                  {preview.pressure_ratio != null
                    ? ` · 含输出预留与余量的压力 ${Math.round(preview.pressure_ratio * 100)}%`
                    : ''}
                </span>
                {preview.inactive_skills?.map((skill) => (
                  <small key={skill.skill_id}>
                    {skill.name} v{skill.version} · {skill.reason}
                  </small>
                ))}
                <small>
                  这是估算，不是模型实际用量。未计入：{preview.unaccounted_items.join('、')}。
                </small>
              </>
            )}
            {error ? (
              <span>{error}</span>
            ) : !usage ? (
              <span>正在读取实际用量</span>
            ) : usage.status === 'no_calls' ? (
              <>
                <strong>当前会话尚无对话模型调用</strong>
                <span>实际输入 0 tokens</span>
              </>
            ) : usage.status === 'unknown' ? (
              <>
                <strong>实际输入未知</strong>
                <span>最近一次调用未返回用量，不能按估算值代替。</span>
              </>
            ) : (
              <>
                <strong>最近一次模型调用的实际输入</strong>
                <span>输入 {usage.input_tokens?.toLocaleString()} tokens</span>
                <span>
                  {usage.effective_window
                    ? `当前配置窗口 ${usage.effective_window.toLocaleString()}`
                    : '模型或窗口配置已变化，占比暂不可用'}
                </span>
                <small>
                  只显示当前会话最近一次模型返回的输入用量，不累计历史调用；内容变化或压缩后，需再次调用才能取得新的实际值。
                </small>
              </>
            )}
          </div>,
          document.body
        )}
    </div>
  );
}
