import { useEffect, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import { api } from './api';
import type { ResultCard } from './types';

export function KnowledgeAnswerText({ text, cards }: { text: string; cards: ResultCard[] }) {
  const ids = cards.flatMap((card) =>
    card.kind === 'knowledge_quote' && card.reference_label ? [card.citation_id] : []
  );
  const key = JSON.stringify(ids);
  const [checked, setChecked] = useState<{ key: string; valid: boolean } | null>(null);
  useEffect(() => {
    if (!ids.length) return;
    const controller = new AbortController();
    let generation = 0;
    const verify = async () => {
      const current = ++generation;
      try {
        await Promise.all(ids.map((id) => api(`/evidence/${id}`, { signal: controller.signal })));
        if (!controller.signal.aborted && current === generation) setChecked({ key, valid: true });
      } catch {
        if (!controller.signal.aborted && current === generation) setChecked({ key, valid: false });
      }
    };
    void verify();
    const timer = window.setInterval(() => void verify(), 5000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [key]);
  if (ids.length && checked?.key !== key) return <p className="muted">正在核对知识依据…</p>;
  if (ids.length && !checked?.valid)
    return <p role="status">知识依据已变化或暂不可用，请重新查询；其他业务卡片仍可核对。</p>;
  return <div className="prose"><ReactMarkdown>{text}</ReactMarkdown></div>;
}
