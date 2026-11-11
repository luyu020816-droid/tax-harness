import { useEffect, useState } from 'react';
import { api } from './api';
import './context-indicator.css';

interface Usage {
  status: 'no_calls' | 'measured' | 'unknown';
  input_tokens: number | null;
  effective_window: number | null;
  model?: string;
}

export function ContextIndicator({ sessionId, version }: { sessionId: string | null; version: string }) {
  const [usage, setUsage] = useState<Usage | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    setUsage(null);
    setError('');
    if (!sessionId) return;
    const controller = new AbortController();
    api<Usage>(`/sessions/${sessionId}/context-usage`, { signal: controller.signal })
      .then((result) => { if (!controller.signal.aborted) setUsage(result); })
      .catch((failure) => { if (!controller.signal.aborted) setError(failure.message || '用量暂不可用'); });
    return () => controller.abort();
  }, [sessionId, version]);
  const ratio = usage?.effective_window && usage.input_tokens != null
    ? usage.input_tokens / usage.effective_window : null;
  const percent = ratio == null ? null : Math.round(ratio * 100);
  const ring = Math.min(100, Math.max(0, percent ?? 0));
  const label = percent == null ? '上下文用量 --' : `上下文用量 ${percent}%`;
  return (
    <details className="context-indicator">
      <summary aria-label={`${label}，点击查看详情`}>
        <span aria-hidden="true" className="context-ring" style={{
          background: `conic-gradient(${ring >= 90 ? '#cf7a35' : '#5874cc'} ${ring}%, #dfe5ee 0)`,
        }}><span>{percent == null ? '--' : `${percent}%`}</span></span>
        <span>实际输入</span>
      </summary>
      <div className="context-indicator-popover" role="status">
        {error ? <span>{error}</span> : !usage ? <span>正在读取实际用量</span> :
          usage.status === 'no_calls' ? <><strong>当前会话尚无对话模型调用</strong><span>实际输入 0 tokens</span></> :
          usage.status === 'unknown' ? <><strong>实际输入未知</strong><span>最近一次调用未返回用量，不能按估算值代替。</span></> : <>
            <strong>最近一次模型调用的实际输入</strong>
            <span>输入 {usage.input_tokens?.toLocaleString()} tokens</span>
            <span>{usage.effective_window ? `当前配置窗口 ${usage.effective_window.toLocaleString()}` : '模型或窗口配置已变化，占比暂不可用'}</span>
            <small>只显示当前会话最近一次模型返回的输入用量，不累计历史调用；内容变化或压缩后，需再次调用才能取得新的实际值。</small>
          </>}
      </div>
    </details>
  );
}
