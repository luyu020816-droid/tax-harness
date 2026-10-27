import { useRef, useState, type FormEvent } from 'react';
import { ArrowUpRight, FileText, Pencil, Plus, Trash2 } from 'lucide-react';
import { api, body, companyName, requestId, statusName, taskName } from './api';
import { Modal } from './Modal';
import { useResource } from './useResource';
import type { Detail, Pending, Preference, Task, User } from './types';

function PreferenceForm({
  initial,
  user,
  company,
  onClose,
  onSaved,
}: {
  initial?: Preference;
  user: User;
  company: string | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [value, setValue] = useState(initial?.value ?? '');
  const [scope, setScope] = useState(initial?.scope_key ?? '@user');
  const [key, setKey] = useState(initial?.stable_key ?? 'result_order');
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const identity = useRef(requestId());
  const edited = () => {
    identity.current = requestId();
  };
  async function submit(event: FormEvent) {
    event.preventDefault();
    setSaving(true);
    setError('');
    try {
      await api(initial ? `/preferences/${initial.preference_id}` : '/preferences', {
        method: initial ? 'PATCH' : 'POST',
        body: body({
          request_id: identity.current,
          scope,
          key: initial ? undefined : key,
          value,
          expected_revision: initial?.revision,
        }),
      });
      onSaved();
      onClose();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSaving(false);
    }
  }
  return (
    <Modal title={initial ? '更正偏好' : '记住一个偏好'} onClose={onClose}>
      <form onSubmit={submit} className="form-stack">
        <p className="muted">只保存你希望以后持续使用的服务偏好。</p>
        <label>
          作用范围
          <select
            disabled={Boolean(initial) || saving}
            value={scope}
            onChange={(event) => {
              setScope(event.target.value);
              edited();
            }}
          >
            <option value="@user">我的全部企业</option>
            {user.companies.map((c) => (
              <option key={c.company_id} value={c.company_id}>
                {companyName(c.company_id)}
                {company === c.company_id ? '（当前）' : ''}
              </option>
            ))}
          </select>
        </label>
        <label>
          偏好类型
          <select
            disabled={Boolean(initial) || saving}
            value={key}
            onChange={(event) => {
              setKey(event.target.value);
              edited();
            }}
          >
            <option value="result_order">结果展示顺序</option>
            <option value="response_style">回复方式</option>
            <option value="list_format">列表格式</option>
            {initial && !['result_order', 'response_style', 'list_format'].includes(key) && (
              <option value={key}>已保存的偏好</option>
            )}
          </select>
        </label>
        <label>
          具体要求
          <textarea
            data-autofocus
            value={value}
            required
            disabled={saving}
            onChange={(event) => {
              setValue(event.target.value);
              edited();
            }}
            placeholder="例如：检查后先列需要补用途的发票"
            rows={4}
          />
        </label>
        <span className={value.length > 1000 ? 'error-note small' : 'muted small'}>
          {value.length} / 1000 字符{value.length > 1000 ? '，请缩短后保存' : ''}
        </span>
        {error && (
          <p className="error-note" role="alert">
            {error}
          </p>
        )}
        <footer className="form-actions">
          <button type="button" className="secondary" onClick={onClose}>
            取消
          </button>
          <button className="primary" disabled={saving || !value.trim() || value.length > 1000}>
            {saving ? '正在保存…' : '保存偏好'}
          </button>
        </footer>
      </form>
    </Modal>
  );
}

function TaskForm({
  initial,
  user,
  onClose,
  onSaved,
}: {
  initial: Task;
  user: User;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [company, setCompany] = useState(initial.company_id);
  const [period, setPeriod] = useState(initial.period ?? '');
  const [intent, setIntent] = useState(initial.intent);
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const identity = useRef(requestId());
  async function submit(event: FormEvent) {
    event.preventDefault();
    setSaving(true);
    setError('');
    try {
      await api(`/tasks/${initial.task_id}`, {
        method: 'PATCH',
        body: body({
          request_id: identity.current,
          expected_revision: initial.revision,
          company_id: company,
          period: period || null,
          intent,
          action: company === initial.company_id ? 'update' : 'replace',
        }),
      });
      onSaved();
      onClose();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSaving(false);
    }
  }
  const edited = () => {
    identity.current = requestId();
  };
  return (
    <Modal title="更正办理范围" onClose={onClose}>
      <form onSubmit={submit} className="form-stack">
        <label>
          企业
          <select
            value={company}
            disabled={saving}
            onChange={(event) => {
              setCompany(event.target.value);
              edited();
            }}
          >
            {user.companies.map((c) => (
              <option key={c.company_id} value={c.company_id}>
                {companyName(c.company_id)}
              </option>
            ))}
          </select>
        </label>
        <label>
          月份
          <input
            type="month"
            value={period}
            disabled={saving}
            onChange={(event) => {
              setPeriod(event.target.value);
              edited();
            }}
          />
        </label>
        <label>
          办理目标
          <select
            value={intent}
            disabled={saving}
            onChange={(event) => {
              setIntent(event.target.value);
              edited();
            }}
          >
            <option value="complete_purposes">发票用途检查与补全</option>
            <option value="query_month">月度资料查询</option>
            <option value="explain_rules">规则查询</option>
          </select>
        </label>
        {company !== initial.company_id && (
          <p className="notice">更换企业将替换这项任务，已保存的发票仍保留原归属。</p>
        )}
        {error && (
          <p className="error-note" role="alert">
            {error}
          </p>
        )}
        <footer className="form-actions">
          <button type="button" className="secondary" onClick={onClose}>
            取消
          </button>
          <button className="primary" disabled={saving}>
            {saving ? '正在保存…' : '保存更正'}
          </button>
        </footer>
      </form>
    </Modal>
  );
}

export function MemoryView({
  user,
  company,
  onDetail,
  onDraft,
  onChanged,
}: {
  user: User;
  company: string | null;
  onDetail: (detail: Detail) => void;
  onDraft: (text: string) => void;
  onChanged: () => void;
}) {
  const [tab, setTab] = useState('tasks');
  const suffix = company ? `?company_id=${company}` : '';
  const taskResource = useResource<Task[]>('/tasks' + suffix);
  const prefResource = useResource<Preference[]>('/preferences' + suffix);
  const pendingResource = useResource<Pending[]>('/pending-clarifications');
  const [prefForm, setPrefForm] = useState<Preference | 'new' | null>(null);
  const [taskForm, setTaskForm] = useState<Task | null>(null);
  const [confirm, setConfirm] = useState<{
    title: string;
    description: string;
    action: () => Promise<unknown>;
  } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const refresh = () => {
    taskResource.reload();
    prefResource.reload();
    pendingResource.reload();
    onChanged();
  };
  const confirmDelete = (title: string, description: string, path: string, revision: number) => {
    const request_id = requestId();
    setError('');
    setConfirm({
      title,
      description,
      action: () =>
        api(path, { method: 'DELETE', body: body({ request_id, expected_revision: revision }) }),
    });
  };
  async function remove() {
    if (!confirm) return;
    setBusy(true);
    setError('');
    try {
      await confirm.action();
      setConfirm(null);
      refresh();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function cancel(task: Task) {
    setBusy(true);
    setError('');
    try {
      await api(`/tasks/${task.task_id}`, {
        method: 'PATCH',
        body: body({ request_id: requestId(), action: 'cancel', expected_revision: task.revision }),
      });
      refresh();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const tasksList = taskResource.data ?? [],
    prefs = prefResource.data ?? [];
  const pending = (pendingResource.data ?? []).filter(
    (p) =>
      !p.pending_clarification.task_id &&
      (!company ||
        !p.pending_clarification.company_id ||
        p.pending_clarification.company_id === company)
  );
  const active = tasksList.filter((t) => ['active', 'waiting_user'].includes(t.status));
  const ended = tasksList.filter((t) => !['active', 'waiting_user'].includes(t.status));
  const taskRow = (task: Task) => (
    <article className="memory-row" key={task.task_id}>
      <div className="memory-content">
        <button className="row-title" onClick={() => onDetail({ kind: 'task', data: task })}>
          {taskName(task.intent)}
        </button>
        <p>
          {companyName(task.company_id)} · {task.period ?? '月份待确认'}
        </p>
        <span className={`badge ${task.status === 'completed' ? 'success' : 'quiet'}`}>
          {statusName(task.status)}
        </span>
      </div>
      <div className="row-actions">
        {['active', 'waiting_user'].includes(task.status) && (
          <>
            <button
              className="text-button"
              onClick={() =>
                onDraft(
                  `继续处理${companyName(task.company_id)} ${task.period ?? ''} 的${taskName(task.intent)}任务`
                )
              }
            >
              继续
              <ArrowUpRight size={14} />
            </button>
            <button className="icon-button" aria-label="更正任务" onClick={() => setTaskForm(task)}>
              <Pencil size={16} />
            </button>
            <button className="text-button muted" disabled={busy} onClick={() => cancel(task)}>
              取消
            </button>
          </>
        )}
        <button
          className="icon-button"
          aria-label="删除任务"
          onClick={() =>
            confirmDelete(
              '删除这项任务？',
              '该任务将停止自动续办。已保存的发票资料不会撤销。',
              `/tasks/${task.task_id}`,
              task.revision
            )
          }
        >
          <Trash2 size={16} />
        </button>
      </div>
    </article>
  );
  return (
    <section className="management-page">
      <div className="page-heading">
        <div>
          <h1>记忆管理</h1>
          <p>查看当前进度，让系统按你的最新要求继续。</p>
        </div>
        {tab === 'preferences' && (
          <button className="primary" onClick={() => setPrefForm('new')}>
            <Plus size={16} />
            记住偏好
          </button>
        )}
      </div>
      <div className="tabs page-tabs">
        {[
          ['tasks', '任务', active.length],
          ['preferences', '偏好', prefs.length],
          ['pending', '待澄清', pending.length],
        ].map(([key, label, count]) => (
          <button
            key={key}
            className={tab === key ? 'selected' : ''}
            onClick={() => setTab(String(key))}
          >
            {label}
            <span>{count}</span>
          </button>
        ))}
      </div>
      {(error || taskResource.error || prefResource.error || pendingResource.error) && !confirm && (
        <p className="error-note" role="alert">
          {error || taskResource.error || prefResource.error || pendingResource.error}
        </p>
      )}
      {tab === 'tasks' && (
        <>
          {taskResource.loading ? (
            <p className="muted">正在读取任务…</p>
          ) : active.length ? (
            <div className="memory-list">{active.map(taskRow)}</div>
          ) : (
            <div className="empty-panel">
              <h3>当前没有未完成任务</h3>
              <p>开始一次检查或提出查询后，进度会在需要时保存。</p>
            </div>
          )}
          {ended.length > 0 && (
            <details className="ended-tasks">
              <summary>最近已结束的任务（{ended.length}）</summary>
              {ended.map(taskRow)}
            </details>
          )}
        </>
      )}
      {tab === 'preferences' &&
        (prefResource.loading ? (
          <p className="muted">正在读取偏好…</p>
        ) : prefs.length ? (
          <div className="memory-list">
            {prefs.map((pref) => (
              <article className="memory-row" key={pref.preference_id}>
                <div className="memory-content">
                  <strong>{pref.value}</strong>
                  <p>
                    {pref.scope_key === '@user' ? '我的全部企业' : companyName(pref.scope_key)} ·
                    用户明确设置
                  </p>
                  {pref.source_refs[0] && (
                    <button
                      className="text-button muted small"
                      onClick={() =>
                        onDetail({ kind: 'source', id: pref.source_refs[0].source_id })
                      }
                    >
                      <FileText size={12} />
                      查看来源
                    </button>
                  )}
                </div>
                <div className="row-actions">
                  <button
                    className="icon-button"
                    aria-label="更正偏好"
                    onClick={() => setPrefForm(pref)}
                  >
                    <Pencil size={16} />
                  </button>
                  <button
                    className="icon-button"
                    aria-label="删除偏好"
                    onClick={() =>
                      confirmDelete(
                        '删除这条偏好？',
                        '后续不再自动使用这条偏好；历史原文仍可用于核对。',
                        `/preferences/${pref.preference_id}`,
                        pref.revision
                      )
                    }
                  >
                    <Trash2 size={16} />
                  </button>
                </div>
              </article>
            ))}
          </div>
        ) : (
          <div className="empty-panel">
            <h3>还没有长期偏好</h3>
            <p>明确告诉我以后怎样回复，也可以在这里添加。</p>
            <button className="secondary" onClick={() => setPrefForm('new')}>
              <Plus size={15} />
              添加偏好
            </button>
          </div>
        ))}
      {tab === 'pending' &&
        (pendingResource.loading ? (
          <p className="muted">正在读取待澄清事项…</p>
        ) : pending.length ? (
          <div className="memory-list">
            {pending.map((item) => (
              <article className="memory-row" key={item.turn_id}>
                <div className="memory-content">
                  <strong>{item.pending_clarification.summary}</strong>
                  <p>
                    {item.pending_clarification.company_id
                      ? companyName(item.pending_clarification.company_id)
                      : '企业待确认'}{' '}
                    · {item.pending_clarification.period ?? '月份待确认'}
                  </p>
                </div>
                <div className="row-actions">
                  <button
                    className="text-button"
                    onClick={() => onDraft('继续处理之前尚未明确的事项')}
                  >
                    继续
                    <ArrowUpRight size={14} />
                  </button>
                  <button
                    className="icon-button"
                    aria-label="删除待澄清事项"
                    onClick={() =>
                      confirmDelete(
                        '删除这项待澄清事项？',
                        '这项问题将停止恢复，已经保存的业务资料保留。',
                        `/turns/${item.turn_id}/pending-clarification`,
                        item.pending_clarification.revision
                      )
                    }
                  >
                    <Trash2 size={16} />
                  </button>
                </div>
              </article>
            ))}
          </div>
        ) : (
          <div className="empty-panel">
            <h3>没有需要澄清的事项</h3>
            <p>信息不足时，我会只询问办理所缺的内容。</p>
          </div>
        ))}
      {prefForm && (
        <PreferenceForm
          initial={prefForm === 'new' ? undefined : prefForm}
          user={user}
          company={company}
          onClose={() => setPrefForm(null)}
          onSaved={refresh}
        />
      )}
      {taskForm && (
        <TaskForm
          initial={taskForm}
          user={user}
          onClose={() => setTaskForm(null)}
          onSaved={refresh}
        />
      )}
      {confirm && (
        <Modal title={confirm.title} onClose={() => setConfirm(null)}>
          <p>{confirm.description}</p>
          {error && (
            <p className="error-note" role="alert">
              {error}
            </p>
          )}
          <footer className="form-actions">
            <button className="secondary" onClick={() => setConfirm(null)}>
              保留
            </button>
            <button className="danger-button" disabled={busy} onClick={remove}>
              {busy ? '正在删除…' : '确认删除'}
            </button>
          </footer>
        </Modal>
      )}
    </section>
  );
}
