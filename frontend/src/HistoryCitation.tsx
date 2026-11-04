import { useEffect, useRef, useState } from 'react';
import { api, companyName, dateTime } from './api';
import type { HistoryQuoteCard } from './types';

interface Neighbor {
  source_id: string;
  direction: 'previous' | 'next';
  role: 'user' | 'assistant';
  created_at: string;
}

interface HistoryEvidence {
  citation_id?: string;
  neighbors?: Neighbor[];
  company_ids?: string[];
  role: 'user' | 'assistant' | 'tool';
  source_id?: string;
  session_id?: string;
  next_char_start?: number | null;
  created_at: string;
  excerpt: string;
  locator: { char_start: number; char_end: number };
}

export function HistoryCitation({
  card,
  allowNeighbors = true,
}: {
  card: HistoryQuoteCard;
  allowNeighbors?: boolean;
}) {
  const [evidence, setEvidence] = useState<HistoryEvidence | null>(null);
  const [error, setError] = useState('');
  const [continuation, setContinuation] = useState('');
  const [nextStart, setNextStart] = useState<number | null>(null);
  const [reading, setReading] = useState(false);
  const [neighborCards, setNeighborCards] = useState<HistoryQuoteCard[]>([]);
  const sourceGeneration = useRef(0);
  const hasReadMore = useRef(false);
  useEffect(() => {
    sourceGeneration.current += 1;
    hasReadMore.current = false;
    setNeighborCards([]);
    setContinuation('');
    setNextStart(null);
    setReading(false);
    const controller = new AbortController();
    let revision = 0;
    const refresh = () => {
      const current = ++revision;
      void api<HistoryEvidence>(
        `/evidence/${card.citation_id}${card.session_id ? `?session_id=${card.session_id}` : ''}`,
        { signal: controller.signal }
      )
        .then((value) => {
          if (!controller.signal.aborted && current === revision) {
            setEvidence(value);
            if (!hasReadMore.current) setNextStart(value.next_char_start ?? null);
            setError('');
          }
        })
        .catch((err) => {
          if (!controller.signal.aborted && current === revision) {
            sourceGeneration.current += 1;
            setEvidence(null);
            setNeighborCards([]);
            setContinuation('');
            setNextStart(null);
            setError(err.message);
          }
        });
    };
    setEvidence(null);
    setError('');
    refresh();
    const timer = setInterval(refresh, 5000);
    return () => {
      sourceGeneration.current += 1;
      controller.abort();
      clearInterval(timer);
    };
  }, [card.citation_id, card.session_id]);
  async function readMore() {
    if (nextStart === null || reading) return;
    const stamp = sourceGeneration.current;
    hasReadMore.current = true;
    setReading(true);
    try {
      const value = await api<HistoryEvidence>('/conversations/read', {
        method: 'POST',
        body: JSON.stringify({ source_id: card.source_id, char_start: nextStart }),
      });
      if (stamp !== sourceGeneration.current) return;
      setContinuation((old) => old + value.excerpt);
      setNextStart(value.next_char_start ?? null);
    } catch (err) {
      if (stamp !== sourceGeneration.current) return;
      setEvidence(null);
      setContinuation('');
      setError((err as Error).message);
    } finally {
      if (stamp === sourceGeneration.current) setReading(false);
    }
  }
  async function readNeighbor(neighbor: Neighbor) {
    if (reading) return;
    const stamp = sourceGeneration.current;
    setReading(true);
    try {
      const value = await api<HistoryEvidence>('/conversations/read', {
        method: 'POST',
        body: JSON.stringify({ source_id: neighbor.source_id, char_start: 0 }),
      });
      if (stamp !== sourceGeneration.current) return;
      if (!value.citation_id || !value.source_id) throw new Error('原文引用不完整');
      setNeighborCards((old) => [
        ...old,
        {
          kind: 'history_quote',
          citation_id: value.citation_id!,
          source_id: value.source_id!,
          session_id: value.session_id,
          company_id: card.company_id,
          quote_start: value.locator.char_start,
          quote_end: value.locator.char_end,
        },
      ]);
    } catch (err) {
      if (stamp !== sourceGeneration.current) return;
      setError((err as Error).message);
      setNeighborCards([]);
      setEvidence(null);
      setContinuation('');
    } finally {
      if (stamp === sourceGeneration.current) setReading(false);
    }
  }
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
      {continuation && (
        <blockquote className="knowledge-excerpt history-excerpt">{continuation}</blockquote>
      )}
      {nextStart !== null && (
        <button className="secondary" disabled={reading} onClick={() => void readMore()}>
          {reading ? '正在核对原文…' : '继续读取这条原文'}
        </button>
      )}
      {allowNeighbors &&
        evidence.neighbors?.map((neighbor) => (
          <button
            key={neighbor.source_id}
            className="secondary"
            disabled={reading || neighborCards.some((c) => c.source_id === neighbor.source_id)}
            onClick={() => void readNeighbor(neighbor)}
          >
            查看{neighbor.direction === 'previous' ? '上一条' : '下一条'}
            {neighbor.role === 'user' ? '用户' : '助手'}原文
          </button>
        ))}
      {neighborCards.map((neighborCard) => (
        <HistoryCitation
          key={neighborCard.citation_id}
          card={neighborCard}
          allowNeighbors={false}
        />
      ))}
      {evidence.session_id && (
        <a
          className="muted small"
          href={`${window.location.pathname}?session=${encodeURIComponent(evidence.session_id)}`}
        >
          打开原会话
        </a>
      )}
      <p className="muted small">
        字符 {card.quote_start + 1}—{card.quote_end} · 当时记录，当前业务状态需重新查询
      </p>
    </section>
  );
}
