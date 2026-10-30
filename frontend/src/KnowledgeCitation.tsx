import { useEffect, useState } from 'react';
import { FileText, ExternalLink } from 'lucide-react';
import { api } from './api';
import type { Detail, KnowledgeEvidence } from './types';

export function locatorLabel(items: KnowledgeEvidence['locator']) {
  return [
    ...new Set(
      items.map((item) =>
        item.type === 'pdf'
          ? `第${item.page}页`
          : item.type === 'lines'
            ? `第${item.from}—${item.to}行`
            : item.type === 'docx_paragraph'
              ? `第${item.paragraph}段`
              : `表${item.table}第${item.row}行（含表头）`
      )
    ),
  ].join('、');
}

export function useKnowledgeEvidence(id: string) {
  const [evidence, setEvidence] = useState<KnowledgeEvidence | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    let request = 0;
    setEvidence(null);
    setError('');
    const refresh = () => {
      const current = ++request;
      return api<KnowledgeEvidence>(`/evidence/${id}`, { signal: controller.signal })
        .then((value) => {
          if (!controller.signal.aborted && current === request) {
            setEvidence(value);
            setError('');
          }
        })
        .catch((err) => {
          if (!controller.signal.aborted && current === request) {
            setEvidence(null);
            setError(err.message);
          }
        });
    };
    void refresh();
    const timer = setInterval(() => void refresh(), 5000);
    return () => {
      controller.abort();
      clearInterval(timer);
    };
  }, [id]);
  return { evidence, error };
}

export function KnowledgeCitation({
  id,
  compact = false,
  onDetail,
}: {
  id: string;
  compact?: boolean;
  onDetail: (detail: Detail) => void;
}) {
  const { evidence, error } = useKnowledgeEvidence(id);
  if (error)
    return (
      <p className="error-note" role="status">
        引用当前不可用：{error}
      </p>
    );
  if (!evidence) return <p className="muted small">正在核对引用有效性…</p>;
  return (
    <section className={`knowledge-citation ${compact ? 'compact' : ''}`}>
      <button className="text-button" onClick={() => onDetail({ kind: 'evidence', id })}>
        <FileText size={16} />
        <strong>{evidence.title}</strong>
        <span>
          第{evidence.edition}版 · {locatorLabel(evidence.locator)}
        </span>
        <ExternalLink size={13} />
      </button>
      {!compact && <blockquote className="knowledge-excerpt">{evidence.excerpt}</blockquote>}
    </section>
  );
}
