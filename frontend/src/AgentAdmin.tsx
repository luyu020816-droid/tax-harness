import { useCallback, useEffect, useState, type FormEvent } from 'react';
import { api, body } from './api';
import { Modal } from './Modal';
import type { KnowledgeDocument } from './types';

type Grants = Record<string, string[]>;
type Draft = {
  name: string;
  description: string;
  prompt: string;
  model_profile_id: string;
  tool_grants: Grants;
  skill_ids: string[];
  knowledge_mode: string;
  knowledge_document_ids: string[];
};
type Agent = {
  agent_id: string;
  name: string;
  revision: number;
  current_version: number | null;
  enabled: boolean;
  draft: Draft;
  published_config?: Draft | null;
};
type Tool = {
  name: string;
  description: string;
  actions: string[];
  enabled: boolean;
  revision: number;
  implementation_version: string;
  contract_version: string;
  risk_type: 'read_only' | 'side_effect';
};
type Skill = {
  skill_id: string;
  name: string;
  revision: number;
  current_version: number;
  enabled: boolean;
  config: SkillDraft;
};
type SkillDraft = {
  name: string;
  description: string;
  instructions: string;
  tool_grants: Grants;
  requires_knowledge: boolean;
};
type Model = {
  profile_id: string;
  name: string;
  version: number;
  model_id: string;
  base_url: string;
  configured: boolean;
};
const emptyAgent = (): Draft => ({
  name: '',
  description: '',
  prompt: '',
  model_profile_id: 'default',
  tool_grants: {},
  skill_ids: [],
  knowledge_mode: 'none',
  knowledge_document_ids: [],
});
const emptySkill = (): SkillDraft => ({
  name: '',
  description: '',
  instructions: '',
  tool_grants: {},
  requires_knowledge: false,
});

function toggleGrant(grants: Grants, name: string, action: string): Grants {
  const next = new Set(grants[name] ?? []);
  if (next.has(action)) next.delete(action);
  else next.add(action);
  const result = { ...grants };
  if (next.size) result[name] = [...next];
  else delete result[name];
  return result;
}

function GrantPicker({
  tools,
  grants,
  onChange,
}: {
  tools: Tool[];
  grants: Grants;
  onChange: (g: Grants) => void;
}) {
  return (
    <details>
      <summary>工具和动作授权（{Object.keys(grants).length} 项）</summary>
      {tools.map((tool) => (
        <fieldset key={tool.name}>
          <legend>
            {tool.name}
            {tool.enabled ? '' : '（已停用）'}
          </legend>
          <p className="muted small">{tool.description}</p>
          {tool.actions.map((action) => (
            <label key={action} style={{ display: 'inline-block', marginRight: 12 }}>
              <input
                type="checkbox"
                checked={(grants[tool.name] ?? []).includes(action)}
                onChange={() => onChange(toggleGrant(grants, tool.name, action))}
              />{' '}
              {action}
            </label>
          ))}
        </fieldset>
      ))}
    </details>
  );
}

export function AgentAdmin({ company, onClose }: { company: string | null; onClose: () => void }) {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [tools, setTools] = useState<Tool[]>([]);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [models, setModels] = useState<Model[]>([]);
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
  const [documentOffset, setDocumentOffset] = useState<number | null>(null);
  const [draft, setDraft] = useState<Draft>(emptyAgent);
  const [selected, setSelected] = useState<Agent | null>(null);
  const [skill, setSkill] = useState<SkillDraft>(emptySkill);
  const [selectedSkill, setSelectedSkill] = useState<Skill | null>(null);
  const [modelId, setModelId] = useState('');
  const [modelName, setModelName] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [providerModel, setProviderModel] = useState('');
  const [key, setKey] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const load = useCallback(async () => {
    const [a, t, s, m] = await Promise.all([
      api<{ agents: Agent[] }>('/agent-admin/agents'),
      api<Tool[]>('/agent-admin/tools'),
      api<Skill[]>('/agent-admin/skills'),
      api<Model[]>('/agent-admin/models'),
    ]);
    setAgents(a.agents);
    setTools(t);
    setSkills(s);
    setModels(m);
    if (company) {
      const result = await api<{ documents: KnowledgeDocument[]; next_offset?: number | null }>(
        `/knowledge/documents?company_id=${encodeURIComponent(company)}`
      );
      setDocuments(result.documents);
      setDocumentOffset(result.next_offset ?? null);
    } else {
      setDocuments([]);
      setDocumentOffset(null);
    }
  }, [company]);
  useEffect(() => {
    void load().catch((e: Error) => setError(e.message));
  }, [load]);
  async function run<T>(
    action: () => Promise<T>,
    success: string | ((value: T) => string),
    refresh = true
  ) {
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const result = await action();
      if (refresh) await load();
      setNotice(typeof success === 'function' ? success(result) : success);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function saveAgent(event: FormEvent) {
    event.preventDefault();
    await run(async () => {
      const result = selected
        ? await api<Agent>(`/agent-admin/agents/${selected.agent_id}`, {
            method: 'PUT',
            body: body({ expected_revision: selected.revision, draft }),
          })
        : await api<Agent>('/agent-admin/agents', { method: 'POST', body: body(draft) });
      setSelected(result);
      setDraft(result.draft);
    }, '草稿已保存；发布后才会生效。');
  }
  async function saveSkill(event: FormEvent) {
    event.preventDefault();
    await run(async () => {
      const result = selectedSkill
        ? await api<Skill>(`/agent-admin/skills/${selectedSkill.skill_id}`, {
            method: 'PUT',
            body: body({ expected_revision: selectedSkill.revision, draft: skill }),
          })
        : await api<Skill>('/agent-admin/skills', { method: 'POST', body: body(skill) });
      setSelectedSkill(result);
      setSkill(result.config);
    }, 'Skill 版本已发布。');
  }
  async function saveModel(event: FormEvent) {
    event.preventDefault();
    const input = {
      name: modelName,
      expected_version: models.find((m) => m.profile_id === modelId)?.version ?? 0,
      connection: { base_url: baseUrl, model_id: providerModel, api_key: key },
    };
    setKey('');
    await run(
      () =>
        api(`/agent-admin/models/${encodeURIComponent(modelId)}/test`, {
          method: 'POST',
          body: body(input),
        }),
      '模型连接测试通过。重启后需要重新输入密钥。'
    );
  }
  const agentDirty = !!selected && JSON.stringify(draft) !== JSON.stringify(selected.draft);
  function affectedAgents(references: (config: Draft) => boolean) {
    return (
      agents
        .filter(
          (a) => references(a.draft) || (!!a.published_config && references(a.published_config))
        )
        .map((a) => `${a.name}（${a.enabled ? '启用' : '停用'}）`)
        .join('、') || '无'
    );
  }
  return (
    <Modal title="Agent 与能力管理" onClose={onClose} drawer>
      <p className="notice">配置由本机模拟管理员管理。Agent 只在当前用户和企业授权范围内工作。</p>
      {error && (
        <p className="error-note" role="alert">
          {error}
        </p>
      )}
      {notice && (
        <p className="notice" role="status">
          {notice}
        </p>
      )}
      <details open>
        <summary>Agent 配置</summary>
        <p>
          <button
            type="button"
            className="secondary"
            onClick={() => {
              setSelected(null);
              setDraft(emptyAgent());
            }}
          >
            新建 Agent
          </button>
        </p>
        {agents.map((a) => (
          <p key={a.agent_id}>
            {a.name} · {a.enabled ? '启用' : '停用'} ·{' '}
            {a.current_version ? `版本 ${a.current_version}` : '未发布'}{' '}
            <button
              className="text-button"
              onClick={() => {
                setSelected(a);
                setDraft(a.draft);
              }}
            >
              编辑
            </button>{' '}
            <button
              className="text-button"
              disabled={busy || !a.current_version}
              onClick={() =>
                void run(async () => {
                  const updated = await api<Agent>(`/agent-admin/agents/${a.agent_id}/enabled`, {
                    method: 'POST',
                    body: body({ expected_revision: a.revision, enabled: !a.enabled }),
                  });
                  // Advance only the version this editor actually loaded. A list refresh
                  // may contain another manager's edits that are absent from this form.
                  setSelected((current) =>
                    current?.agent_id === a.agent_id && current.revision === a.revision
                      ? updated
                      : current
                  );
                }, 'Agent 状态已更新。')
              }
            >
              {a.enabled ? '停用' : '启用'}
            </button>
          </p>
        ))}
        <form className="form-stack" onSubmit={saveAgent}>
          <label>
            名称
            <input
              required
              value={draft.name}
              maxLength={80}
              onChange={(e) => setDraft({ ...draft, name: e.target.value })}
            />
          </label>
          <label>
            职责说明
            <input
              value={draft.description}
              onChange={(e) => setDraft({ ...draft, description: e.target.value })}
            />
          </label>
          <label>
            职责提示词
            <textarea
              value={draft.prompt}
              onChange={(e) => setDraft({ ...draft, prompt: e.target.value })}
            />
          </label>
          <label>
            模型
            <select
              value={draft.model_profile_id}
              onChange={(e) => setDraft({ ...draft, model_profile_id: e.target.value })}
            >
              <option value="default">默认模型</option>
              {models.map((m) => (
                <option key={m.profile_id} value={m.profile_id}>
                  {m.name} · {m.model_id}
                  {m.configured ? '' : '（待配置）'}
                </option>
              ))}
            </select>
          </label>
          <label>
            知识范围
            <select
              value={draft.knowledge_mode}
              onChange={(e) =>
                setDraft({ ...draft, knowledge_mode: e.target.value, knowledge_document_ids: [] })
              }
            >
              <option value="none">不使用知识</option>
              <option value="authorized">当前用户有权访问的知识</option>
              <option value="selected">指定资料</option>
            </select>
          </label>
          {draft.knowledge_mode === 'selected' && (
            <fieldset>
              <legend>当前企业可选资料</legend>
              {documents
                .filter((d) => d.status === 'active' && d.active_version_id)
                .map((d) => (
                  <label key={d.document_id} style={{ display: 'block' }}>
                    <input
                      type="checkbox"
                      checked={draft.knowledge_document_ids.includes(d.document_id)}
                      onChange={(e) =>
                        setDraft({
                          ...draft,
                          knowledge_document_ids: e.target.checked
                            ? [...draft.knowledge_document_ids, d.document_id]
                            : draft.knowledge_document_ids.filter((id) => id !== d.document_id),
                        })
                      }
                    />{' '}
                    {d.title}
                  </label>
                ))}
              {!documents.length && <p className="muted">当前企业没有可选资料。</p>}
              {documentOffset !== null && company && (
                <button
                  type="button"
                  className="secondary"
                  disabled={busy}
                  onClick={() =>
                    void run(
                      async () => {
                        const result = await api<{
                          documents: KnowledgeDocument[];
                          next_offset: number | null;
                        }>(
                          `/knowledge/documents?company_id=${encodeURIComponent(company)}&offset=${documentOffset}`
                        );
                        setDocuments((current) => [...current, ...result.documents]);
                        setDocumentOffset(result.next_offset);
                      },
                      '已加载更多资料。',
                      false
                    )
                  }
                >
                  加载更多资料
                </button>
              )}
            </fieldset>
          )}
          <GrantPicker
            tools={tools}
            grants={draft.tool_grants}
            onChange={(tool_grants) => setDraft({ ...draft, tool_grants })}
          />
          <fieldset>
            <legend>复用 Skill</legend>
            {skills.map((s) => (
              <label key={s.skill_id} style={{ display: 'block' }}>
                <input
                  type="checkbox"
                  disabled={!s.enabled}
                  checked={draft.skill_ids.includes(s.skill_id)}
                  onChange={(e) =>
                    setDraft({
                      ...draft,
                      skill_ids: e.target.checked
                        ? [...draft.skill_ids, s.skill_id]
                        : draft.skill_ids.filter((id) => id !== s.skill_id),
                    })
                  }
                />{' '}
                {s.name} · v{s.current_version}
                {s.enabled ? '' : '（停用）'}
              </label>
            ))}
          </fieldset>
          <div>
            {agentDirty && <p className="notice">有未保存修改，请先保存草稿再发布。</p>}
            {selected && (
              <p className="muted small">
                发布仅更新当前 Agent；新请求使用新版本，撤权会阻止旧请求继续执行。
              </p>
            )}
            <button className="primary" disabled={busy}>
              保存草稿
            </button>{' '}
            {selected && (
              <button
                type="button"
                className="secondary"
                disabled={busy || agentDirty}
                onClick={() =>
                  void run(async () => {
                    const a = await api<Agent>(`/agent-admin/agents/${selected.agent_id}/publish`, {
                      method: 'POST',
                      body: body({ expected_revision: selected.revision }),
                    });
                    setSelected(a);
                  }, 'Agent 版本已发布。')
                }
              >
                发布已保存草稿
              </button>
            )}
          </div>
        </form>
      </details>
      <details>
        <summary>Skill 配置</summary>
        <p>
          <button
            type="button"
            className="secondary"
            onClick={() => {
              setSelectedSkill(null);
              setSkill(emptySkill());
            }}
          >
            新建 Skill
          </button>
        </p>
        {skills.map((s) => (
          <p key={s.skill_id}>
            {s.name} · v{s.current_version} · {s.enabled ? '启用' : '停用'}{' '}
            <span className="muted small">
              引用此 Skill 的 Agent（含草稿）：
              {affectedAgents((c) => c.skill_ids.includes(s.skill_id))}。新版本需重新发布 Agent
              后使用；停用会中止仍依赖它的旧请求。{' '}
            </span>
            <button
              className="text-button"
              onClick={() => {
                setSelectedSkill(s);
                setSkill(s.config);
              }}
            >
              编辑
            </button>{' '}
            <button
              className="text-button"
              disabled={busy}
              onClick={() =>
                void run(async () => {
                  const updated = await api<Skill>(`/agent-admin/skills/${s.skill_id}/enabled`, {
                    method: 'POST',
                    body: body({ expected_revision: s.revision, enabled: !s.enabled }),
                  });
                  setSelectedSkill((current) =>
                    current?.skill_id === s.skill_id && current.revision === s.revision
                      ? { ...current, ...updated, config: current.config }
                      : current
                  );
                }, 'Skill 状态已更新。')
              }
            >
              {s.enabled ? '停用' : '启用'}
            </button>
          </p>
        ))}
        <form className="form-stack" onSubmit={saveSkill}>
          {selectedSkill && (
            <p className="notice">
              本次发布关联 Agent：
              {affectedAgents((c) => c.skill_ids.includes(selectedSkill.skill_id))}。现有 Agent
              保留已绑定版本。
            </p>
          )}
          <label>
            Skill 名称
            <input
              required
              value={skill.name}
              onChange={(e) => setSkill({ ...skill, name: e.target.value })}
            />
          </label>
          <label>
            Skill 用途
            <input
              value={skill.description}
              onChange={(e) => setSkill({ ...skill, description: e.target.value })}
            />
          </label>
          <label>
            任务指引
            <textarea
              required
              value={skill.instructions}
              onChange={(e) => setSkill({ ...skill, instructions: e.target.value })}
            />
          </label>
          <label>
            <input
              type="checkbox"
              checked={skill.requires_knowledge}
              onChange={(e) => setSkill({ ...skill, requires_knowledge: e.target.checked })}
            />{' '}
            需要知识资料
          </label>
          <GrantPicker
            tools={tools}
            grants={skill.tool_grants}
            onChange={(tool_grants) => setSkill({ ...skill, tool_grants })}
          />
          <button className="primary" disabled={busy}>
            发布 Skill
          </button>
        </form>
      </details>
      <details>
        <summary>模型配置</summary>
        <p className="muted small">
          密钥仅保留在服务进程内，重启后需要重新输入并发布新的 Agent 版本。
        </p>
        {models.map((m) => (
          <p key={m.profile_id}>
            {m.name} · {m.model_id} · {m.configured ? '就绪' : '待配置'}{' '}
            <span className="muted small">
              引用此模型的 Agent（含草稿）：
              {affectedAgents((c) => c.model_profile_id === m.profile_id)}。{' '}
            </span>
            <button
              className="text-button"
              onClick={() => {
                setModelId(m.profile_id);
                setModelName(m.name);
                setBaseUrl(m.base_url);
                setProviderModel(m.model_id);
              }}
            >
              重新配置
            </button>
          </p>
        ))}
        <form className="form-stack" onSubmit={saveModel}>
          <label>
            配置编号
            <input
              required
              pattern="[a-zA-Z0-9_-]+"
              value={modelId}
              onChange={(e) => setModelId(e.target.value)}
            />
          </label>
          <label>
            显示名称
            <input required value={modelName} onChange={(e) => setModelName(e.target.value)} />
          </label>
          <label>
            API 地址
            <input
              required
              type="url"
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
            />
          </label>
          <label>
            模型名称
            <input
              required
              value={providerModel}
              onChange={(e) => setProviderModel(e.target.value)}
            />
          </label>
          <label>
            API Key
            <input
              required
              type="password"
              autoComplete="off"
              value={key}
              onChange={(e) => setKey(e.target.value)}
            />
          </label>
          <button className="primary" disabled={busy}>
            测试并启用模型
          </button>
        </form>
      </details>
      <details>
        <summary>工具启停</summary>
        {tools.map((t) => (
          <p key={t.name}>
            {t.name} · {t.enabled ? '启用' : '停用'} ·{' '}
            {t.risk_type === 'read_only' ? '只读业务数据' : '可能产生副作用'} · 源码标识{' '}
            {t.implementation_version?.slice(0, 12)} · 契约标识 {t.contract_version?.slice(0, 12)}{' '}
            <span className="muted small">{t.description} </span>
            <span className="muted small">
              引用此工具的 Agent（含草稿）：{affectedAgents((c) => !!c.tool_grants[t.name]?.length)}
              。停用同时约束原助手。{' '}
            </span>
            <button
              className="text-button"
              disabled={busy}
              onClick={() =>
                void run(
                  () =>
                    api<{ affected_agents: { name: string }[] }>(
                      `/agent-admin/tools/${encodeURIComponent(t.name)}/enabled`,
                      {
                        method: 'POST',
                        body: body({ expected_revision: t.revision, enabled: !t.enabled }),
                      }
                    ),
                  (result) =>
                    `工具状态已更新；受影响 Agent：${result.affected_agents.map((a) => a.name).join('、') || '无'}。`
                )
              }
            >
              {t.enabled ? '停用' : '启用'}
            </button>
          </p>
        ))}
      </details>
    </Modal>
  );
}
