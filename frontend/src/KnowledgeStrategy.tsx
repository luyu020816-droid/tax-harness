import { useEffect, useState, type FormEvent } from 'react';
import { api, body } from './api';
import { useResource } from './useResource';

type Config = {
  enabled: boolean;
  rerank: boolean;
  require_calibrated: boolean;
  evidence_budget: number;
  max_groups: number;
};
type Snapshot = {
  revision: number;
  config: Config;
  can_manage: boolean;
  versions: { version: number; config: Config }[];
};

export function KnowledgeStrategy({ company }: { company: string }) {
  const resource = useResource<Snapshot>(`/knowledge/strategies/${encodeURIComponent(company)}`);
  const [draft, setDraft] = useState<Config | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  useEffect(() => {
    setDraft(resource.data?.config ?? null);
  }, [resource.data]);
  async function save(event: FormEvent) {
    event.preventDefault();
    if (!resource.data || !draft) return;
    setSaving(true);
    setError('');
    setNotice('');
    try {
      await api(`/knowledge/strategies/${encodeURIComponent(company)}`, {
        method: 'PUT',
        body: body({ expected_revision: resource.data.revision, config: draft }),
      });
      resource.reload();
      setNotice('检索策略已发布，旧检索需重新执行。');
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  }
  return (
    <details>
      <summary>知识库检索策略{resource.data ? ` · 版本 ${resource.data.revision}` : ''}</summary>
      <p className="muted small">
        配置仅影响当前企业的知识检索；用户权限、Agent
        范围和资料有效状态始终生效。降低证据预算可能只返回部分依据。
      </p>
      {(error || resource.error) && <p role="alert">{error || resource.error}</p>}
      {notice && <p role="status">{notice}</p>}
      {draft && (
        <form className="form-stack" onSubmit={save}>
          <fieldset disabled={saving || !resource.data?.can_manage}>
            <label>
              <input
                type="checkbox"
                checked={draft.enabled}
                onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })}
              />
              启用知识检索
            </label>
            <label>
              <input
                type="checkbox"
                checked={draft.rerank}
                onChange={(e) => setDraft({ ...draft, rerank: e.target.checked })}
              />
              启用候选重排
            </label>
            <label>
              <input
                type="checkbox"
                checked={draft.require_calibrated}
                onChange={(e) => setDraft({ ...draft, require_calibrated: e.target.checked })}
              />
              仅接受已校准的检索结果
            </label>
            <label>
              证据预算（估算 Token）
              <input
                type="number"
                min={256}
                max={6000}
                required
                value={draft.evidence_budget}
                onChange={(e) => setDraft({ ...draft, evidence_budget: Number(e.target.value) })}
              />
            </label>
            <label>
              最多完整条款组
              <input
                type="number"
                min={1}
                max={12}
                required
                value={draft.max_groups}
                onChange={(e) => setDraft({ ...draft, max_groups: Number(e.target.value) })}
              />
            </label>
            {resource.data?.can_manage && <button className="primary">发布检索策略</button>}
          </fieldset>
        </form>
      )}
      {resource.data?.can_manage && (
        <p>
          <button
            disabled={saving}
            onClick={() => {
              setError('');
              resource.reload();
            }}
          >
            重新载入策略
          </button>
        </p>
      )}
      {!!resource.data?.versions.length && (
        <details>
          <summary>最近策略版本</summary>
          {resource.data.versions.map((v) => (
            <p key={v.version}>
              版本 {v.version} · 预算 {v.config.evidence_budget} · 最多 {v.config.max_groups} 组{' '}
              {resource.data?.can_manage && (
                <button
                  disabled={saving}
                  onClick={() => {
                    setDraft(v.config);
                    setNotice(`已载入版本 ${v.version}，发布后将生成新版本。`);
                  }}
                >
                  载入此版本
                </button>
              )}
            </p>
          ))}
        </details>
      )}
    </details>
  );
}
