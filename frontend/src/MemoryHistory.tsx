import { useEffect, useState } from 'react';
import { api, companyName } from './api';
import { useResource } from './useResource';

type Item = {
  preference_id: string;
  scope_key: string;
  stable_key: string;
  status: string;
  revision: number;
};
type Revision = {
  revision: number;
  generation: number;
  status: string;
  value: string | null;
  redacted?: boolean;
};

export function MemoryHistory({ company }: { company: string | null }) {
  const [items, setItems] = useState<Item[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [selected, setSelected] = useState('');
  const [revisions, setRevisions] = useState<Revision[]>([]);
  const [before, setBefore] = useState<number | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const list = useResource<{ items: Item[]; next_id: string | null }>(
    `/memory/preferences${company ? `?company_id=${encodeURIComponent(company)}` : ''}`
  );
  useEffect(() => {
    setItems(list.data?.items ?? []);
    setNext(list.data?.next_id ?? null);
    setSelected('');
    setRevisions([]);
    setBefore(null);
  }, [list.data, company]);
  async function loadHistory(id: string, cursor?: number) {
    setBusy(true);
    setError('');
    try {
      const result = await api<{ revisions: Revision[]; next_revision: number | null }>(
        `/preferences/${id}/history${cursor ? `?before_revision=${cursor}` : ''}`
      );
      setSelected(id);
      setRevisions((previous) => (cursor ? [...previous, ...result.revisions] : result.revisions));
      setBefore(result.next_revision);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section aria-label="长期记忆变更记录">
      <p className="notice">此处仅供你核对历史修订；已更正或删除的旧值不会作为当前偏好自动使用。</p>
      {(error || list.error) && <p role="alert">{error || list.error}</p>}
      {items.map((item) => (
        <article className="memory-row" key={item.preference_id}>
          <div>
            <strong>{item.stable_key}</strong>
            <p>
              {item.scope_key === '@user' ? '我的全部企业' : companyName(item.scope_key)} ·{' '}
              {item.status === 'deleted' ? '已删除' : '有效'} · 修订 {item.revision}
            </p>
          </div>
          <button disabled={busy} onClick={() => void loadHistory(item.preference_id)}>
            查看修订
          </button>
        </article>
      ))}
      {next && (
        <button
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            try {
              const result = await api<{ items: Item[]; next_id: string | null }>(
                `/memory/preferences?${company ? `company_id=${encodeURIComponent(company)}&` : ''}before_id=${next}`
              );
              setItems((p) => [...p, ...result.items]);
              setNext(result.next_id);
            } catch (e) {
              setError((e as Error).message);
            } finally {
              setBusy(false);
            }
          }}
        >
          加载更多记忆
        </button>
      )}
      {selected && (
        <section aria-label="偏好修订">
          <h3>修订历史</h3>
          {revisions.map((r) => (
            <p key={r.revision}>
              修订 {r.revision} · 代次 {r.generation} · {r.status === 'deleted' ? '删除' : '保存'}：
              {r.redacted ? '来源已失效，内容隐藏' : (r.value ?? '不再生效')}
            </p>
          ))}
          {before && (
            <button disabled={busy} onClick={() => void loadHistory(selected, before)}>
              更早修订
            </button>
          )}
        </section>
      )}
    </section>
  );
}
