import { useEffect, useState } from 'react';
import { api, companyName, dateTime } from './api';
import type { HistoryQuoteCard } from './types';

interface HistoryEvidence {
  company_ids?: string[];
  role: 'user' | 'assistant' | 'tool';
  created_at: string;
  excerpt: string;
  locator: { char_start: number; char_end: number };
}

export function HistoryCitation({ card }: { card: HistoryQuoteCard }) {
  const [evidence, setEvidence] = useState<HistoryEvidence | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    let revision = 0;
    const refresh = () => {
      const current = ++revision;
      void api<HistoryEvidence>(`/evidence/${card.citation_id}`, { signal: controller.signal })
        .then((value) => {
          if (!controller.signal.aborted && current === revision) {
            setEvidence(value);
            setError('');
          }
        })
        .catch((err) => {
          if (!controller.signal.aborted && current === revision) {
            setEvidence(null);
            setError(err.message);
          }
        });
    };
    setEvidence(null);
    setError('');
    refresh();
    const timer = setInterval(refresh, 5000);
    return () => {
      controller.abort();
      clearInterval(timer);
    };
  }, [card.citation_id]);
  if (error)
    return (
      <p className="error-note" role="status">
        历史引用当前不可用：{error}
      </p>
    );
  if (!evidence) return <p className="muted small">正在核对历史来源…</p>;
  const who = { user: '用户', assistant: '助手', tool: '工具' }[evidence.role];
  const quote = Array.from(evidence.excerpt)
    .slice(
      card.quote_start - evidence.locator.char_start,
      card.quote_end - evidence.locator.char_start
    )
    .join('');
  return (
    <section className="knowledge-citation history-citation">
      <strong>
        {evidence.company_ids &&
          (evidence.company_ids.length
            ? evidence.company_ids.map(companyName).join(' / ')
            : '个人历史')}{' '}
        · 历史{who}原文 · {dateTime(evidence.created_at)}
      </strong>
      <blockquote className="knowledge-excerpt history-excerpt">{quote}</blockquote>
      <p className="muted small">
        字符 {card.quote_start + 1}—{card.quote_end} · 当时记录，当前业务状态需重新查询
      </p>
    </section>
  );
}
