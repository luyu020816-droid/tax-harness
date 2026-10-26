import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from 'react';
import ReactMarkdown from 'react-markdown';
import {
  ArrowRight,
  ArrowUp,
  BookOpen,
  BrainCircuit,
  Building2,
  CheckCheck,
  ChevronDown,
  ChevronRight,
  CircleAlert,
  ClipboardCheck,
  History,
  Layers3,
  LoaderCircle,
  Menu,
  MessageSquare,
  Plus,
  ShieldCheck,
  Square,
  X,
} from 'lucide-react';
import { api, ApiError, body, companyName, requestId, statusName, taskName } from './api';
import { Details } from './Details';
import { MemoryView } from './MemoryView';
import { Results } from './Results';
import { RunsView } from './RunsView';
import type { Detail, Session, Task, Turn, User } from './types';

type Page = 'chat' | 'memory' | 'runs';

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [booting, setBooting] = useState(true);
  const [session, setSession] = useState<Session | null>(null);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [company, setCompany] = useState<string | null>(null);
  const [page, setPage] = useState<Page>('chat');
  const [turnList, setTurns] = useState<Turn[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [draft, setDraft] = useState('');
  const [error, setError] = useState('');
  const [detail, setDetail] = useState<Detail | null>(null);
  const [navigationOpen, setNavigationOpen] = useState(false);
  const [currentTask, setCurrentTask] = useState<Task | null>(null);
  const [taskError, setTaskError] = useState('');
  const [version, setVersion] = useState(0);
  const [stage, setStage] = useState('正在处理');
  const [sending, setSending] = useState(false);
  const [newMessages, setNewMessages] = useState(false);
  const generation = useRef(0);
  const composer = useRef<HTMLTextAreaElement>(null);
  const scrollArea = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);
  const composing = useRef(false);
  const retryRequest = useRef<{ id: string; text: string; session: string } | null>(null);
  const acknowledged = useRef(new Set<string>());

  const changed = useCallback(() => setVersion((v) => v + 1), []);

  const clearWorkspace = () => {
    generation.current += 1;
    setTurns([]);
    setSessions([]);
    setSession(null);
    setCompany(null);
    setActiveId(null);
    setCurrentTask(null);
    setDetail(null);
    setDraft('');
    setError('');
    setSending(false);
    retryRequest.current = null;
    acknowledged.current.clear();
    setNavigationOpen(false);
    setPage('chat');
  };

  async function openSession(item: Session, currentUser: User, stamp = generation.current) {
    const messages = await api<{ turns: Turn[]; has_more: boolean }>(
      `/sessions/${item.session_id}/messages`
    );
    if (stamp !== generation.current) return;
    setSession(item);
    setCompany(item.selected_company_id);
    setTurns(messages.turns);
    setActiveId(
      messages.turns.find((t) => ['queued', 'running'].includes(t.status))?.turn_id ?? null
    );
    localStorage.setItem(`tax-harness:session:${currentUser.user_id}`, item.session_id);
    setPage('chat');
    setNavigationOpen(false);
    changed();
    stickToBottom.current = true;
  }

  async function initialize(currentUser: User, stamp: number) {
    const list = await api<Session[]>('/sessions');
    if (stamp !== generation.current) return;
    setSessions(list);
    const previous = localStorage.getItem(`tax-harness:session:${currentUser.user_id}`);
    let chosen = list.find((s) => s.session_id === previous) ?? list[0];
    if (!chosen)
      chosen = await api<Session>('/sessions', {
        method: 'POST',
        body: body({ selected_company_id: currentUser.companies[0]?.company_id ?? null }),
      });
    await openSession(chosen, currentUser, stamp);
  }

  useEffect(() => {
    const stamp = generation.current;
    api<User>('/me')
      .then(async (currentUser) => {
        if (stamp !== generation.current) return;
        setUser(currentUser);
        await initialize(currentUser, stamp);
      })
      .catch((err) => {
        if (stamp === generation.current && !(err instanceof ApiError && err.status === 401))
          setError(err.message);
      })
      .finally(() => {
        if (stamp === generation.current) setBooting(false);
      });
    return () => {
      generation.current += 1;
    };
  }, []);

  async function login(userId: string) {
    clearWorkspace();
    setUser(null);
    setBooting(true);
    const stamp = generation.current;
    try {
      await api('/demo/login', { method: 'POST', body: body({ user_id: userId }) });
      const next = await api<User>('/me');
      if (stamp !== generation.current) return;
      setUser(next);
      await initialize(next, stamp);
    } catch (err) {
      if (stamp === generation.current) setError((err as Error).message);
    } finally {
      if (stamp === generation.current) setBooting(false);
    }
  }

  async function newSession(nextCompany = company) {
    if (!user) return;
    generation.current += 1;
    const stamp = generation.current;
    setSession(null);
    setCompany(nextCompany);
    setTurns([]);
    setActiveId(null);
    setCurrentTask(null);
    setDetail(null);
    setDraft('');
    setError('');
    setSending(false);
    retryRequest.current = null;
    setPage('chat');
    setNavigationOpen(false);
    try {
      const created = await api<Session>('/sessions', {
        method: 'POST',
        body: body({ selected_company_id: nextCompany }),
      });
      if (stamp !== generation.current) return;
      const refreshed = await api<Session[]>('/sessions');
      if (stamp !== generation.current) return;
      setSessions(refreshed);
      await openSession(created, user, stamp);
    } catch (err) {
      if (stamp === generation.current) setError((err as Error).message);
    }
  }

  async function chooseCompany(value: string) {
    const next = value || null;
    if (activeId) {
      try {
        await api(`/turns/${activeId}/cancel`, {
          method: 'POST',
          body: body({ request_id: requestId() }),
        });
      } catch {
        setError('上一轮状态暂时无法确认，请在运行记录中核对。');
        return;
      }
    }
    await newSession(next);
  }

  useEffect(() => {
    setCurrentTask(null);
    setTaskError('');
    if (!user || !company) return;
    const controller = new AbortController();
    api<Task[]>(`/tasks?company_id=${company}`, { signal: controller.signal })
      .then((list) => {
        if (!controller.signal.aborted)
          setCurrentTask(list.find((t) => ['active', 'waiting_user'].includes(t.status)) ?? null);
      })
      .catch((err) => {
        if (!controller.signal.aborted) setTaskError(err.message);
      });
    return () => controller.abort();
  }, [user?.user_id, company, version]);

  useEffect(() => {
    if (!activeId || !user || !session) return;
    const stamp = generation.current;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const stream = new EventSource(`/api/v1/turns/${activeId}/events`);
    stream.addEventListener('model_started', () => {
      if (stamp === generation.current) setStage('正在理解与整理本轮信息');
    });
    stream.addEventListener('tool_started', (event) => {
      if (stamp !== generation.current) return;
      try {
        const name = JSON.parse((event as MessageEvent).data).name;
        setStage(
          (
            {
              query_month: '正在查询月度资料',
              find_invoices: '正在定位发票',
              get_rules: '正在读取适用规则',
              check_purposes: '正在检查用途',
              update_invoice: '正在保存并核验',
              manage_preference: '正在更新偏好',
              change_task: '正在更新任务',
              search_history: '正在查找历史依据',
            } as Record<string, string>
          )[name] ?? '正在办理'
        );
      } catch {
        /* Polling remains the source of the visible result. */
      }
    });
    async function poll() {
      try {
        const result = await api<Turn>(`/turns/${activeId}`, { signal: controller.signal });
        if (stamp !== generation.current || controller.signal.aborted) return;
        setTurns((previous) => previous.map((t) => (t.turn_id === result.turn_id ? result : t)));
        if (!['queued', 'running'].includes(result.status)) {
          stream.close();
          setActiveId(null);
          changed();
          if ('selected_company_id' in result) {
            setCompany(result.selected_company_id ?? null);
            setSession((previous) =>
              previous
                ? { ...previous, selected_company_id: result.selected_company_id ?? null }
                : previous
            );
          }
          void api<Session[]>('/sessions')
            .then((list) => {
              if (stamp === generation.current) setSessions(list);
            })
            .catch(() => {});
          return;
        }
      } catch (err) {
        if (!controller.signal.aborted && stamp === generation.current)
          setStage('连接暂时中断，正在核对本轮状态');
      }
      if (!controller.signal.aborted) timer = setTimeout(poll, 850);
    }
    void poll();
    return () => {
      controller.abort();
      clearTimeout(timer);
      stream.close();
    };
  }, [activeId, user?.user_id, session?.session_id]);

  useEffect(() => {
    if (stickToBottom.current)
      scrollArea.current?.scrollTo({ top: scrollArea.current.scrollHeight, behavior: 'auto' });
    else setNewMessages(true);
  }, [turnList, activeId, stage]);

  useEffect(() => {
    for (const turn of turnList)
      for (const card of turn.final_cards ?? []) {
        if (!('view_id' in card) || !card.view_id || acknowledged.current.has(card.view_id))
          continue;
        const viewId = card.view_id;
        acknowledged.current.add(viewId);
        api(`/views/${viewId}/displayed`, {
          method: 'POST',
          body: body({ turn_id: turn.turn_id, event_seq: turn.next_event_seq }),
        }).catch(() => acknowledged.current.delete(viewId));
      }
  }, [turnList]);

  async function send() {
    if (!session || !draft.trim() || activeId || sending || draft.length > 16000) return;
    if (!user?.model_ready) {
      setError('模型暂未连接，请完成本地配置并重启服务后办理。');
      return;
    }
    const stamp = generation.current,
      text = draft;
    const identity =
      retryRequest.current?.text === text && retryRequest.current.session === session.session_id
        ? retryRequest.current
        : { id: requestId(), text, session: session.session_id };
    retryRequest.current = identity;
    setSending(true);
    setError('');
    stickToBottom.current = true;
    try {
      const accepted = await api<Turn>(`/sessions/${session.session_id}/turns`, {
        method: 'POST',
        body: body({ client_request_id: identity.id, text }),
      });
      if (stamp !== generation.current) return;
      setTurns((previous) =>
        previous.some((t) => t.turn_id === accepted.turn_id)
          ? previous.map((t) => (t.turn_id === accepted.turn_id ? accepted : t))
          : [...previous, accepted]
      );
      setDraft('');
      retryRequest.current = null;
      if (['queued', 'running'].includes(accepted.status)) {
        setStage('已收到，正在办理');
        setActiveId(accepted.turn_id);
      } else changed();
    } catch (err) {
      if (stamp === generation.current)
        setError((err as Error).message || '连接中断，输入已保留；重试将使用同一请求编号。');
    } finally {
      if (stamp === generation.current) setSending(false);
    }
  }

  async function stop() {
    if (!activeId) return;
    try {
      await api(`/turns/${activeId}/cancel`, {
        method: 'POST',
        body: body({ request_id: requestId() }),
      });
    } catch (err) {
      setError((err as Error).message);
    }
  }

  const prepareDraft = (text: string) => {
    setPage('chat');
    setDraft(text);
    retryRequest.current = null;
    requestAnimationFrame(() => composer.current?.focus());
  };
  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (
      event.key === 'Enter' &&
      !event.shiftKey &&
      !event.nativeEvent.isComposing &&
      !composing.current
    ) {
      event.preventDefault();
      void send();
    }
  };
  const canUpdate = user?.companies.find((c) => c.company_id === company)?.can_update ?? false;

  if (booting && !user)
    return (
      <div className="boot-screen">
        <div className="brand-symbol">
          <Layers3 size={23} />
        </div>
        <LoaderCircle className="spinning" size={20} />
        <p>正在连接工作台</p>
      </div>
    );
  if (!user)
    return (
      <div className="login-page">
        <div className="login-brand">
          <div className="brand-symbol">
            <Layers3 size={24} />
          </div>
          <span>财税连续办理助手</span>
        </div>
        <section className="login-card">
          <span className="eyebrow">比赛模拟环境</span>
          <h1>资料有依据，办理有进度。</h1>
          <p>查询、补充、检查，让每一步都能接着办。</p>
          <div className="login-options">
            <button onClick={() => login('U001')}>
              <span className="user-avatar">一</span>
              <span>
                <strong>测试用户一</strong>
                <small>甲、乙企业 · 查询与补充</small>
              </span>
              <ArrowRight size={18} />
            </button>
            <button onClick={() => login('U002')}>
              <span className="user-avatar readonly">二</span>
              <span>
                <strong>测试用户二</strong>
                <small>甲企业 · 只读查询</small>
              </span>
              <ArrowRight size={18} />
            </button>
          </div>
          {error && <p className="error-note">{error}</p>}
          <p className="login-footnote">
            <ShieldCheck size={14} />
            全部企业、资料与规则均为比赛模拟内容
          </p>
        </section>
      </div>
    );

  return (
    <div className="app-shell">
      {navigationOpen && (
        <button
          className="nav-backdrop"
          aria-label="关闭导航"
          onClick={() => setNavigationOpen(false)}
        />
      )}
      <aside className={`sidebar ${navigationOpen ? 'open' : ''}`}>
        <div className="sidebar-brand">
          <div className="brand-symbol">
            <Layers3 size={21} />
          </div>
          <div>
            <strong>财税助手</strong>
            <span>连续办理 · 有据可查</span>
          </div>
          <button
            className="icon-button mobile-close"
            aria-label="关闭导航"
            onClick={() => setNavigationOpen(false)}
          >
            <X size={18} />
          </button>
        </div>
        <button className="new-chat" aria-label="新对话" onClick={() => newSession()}>
          <Plus size={18} />
          <span>新对话</span>
        </button>
        <nav>
          {(
            [
              { id: 'chat', label: '对话', Icon: MessageSquare },
              { id: 'memory', label: '记忆管理', Icon: BrainCircuit },
              { id: 'runs', label: '运行记录', Icon: History },
            ] as const
          ).map(({ id, label, Icon }) => (
            <button
              key={id}
              className={page === id ? 'selected' : ''}
              onClick={() => {
                setPage(id);
                setNavigationOpen(false);
              }}
              title={label}
            >
              <Icon size={18} />
              <span>{label}</span>
              {page === id && <span className="nav-dot" />}
            </button>
          ))}
        </nav>
        <div className="recent-section">
          <span className="nav-label">最近会话</span>
          {sessions.slice(0, 8).map((item) => (
            <button
              key={item.session_id}
              className={item.session_id === session?.session_id ? 'active' : ''}
              onClick={() => {
                if (item.session_id === session?.session_id) {
                  setPage('chat');
                  setNavigationOpen(false);
                  return;
                }
                generation.current += 1;
                setTurns([]);
                setActiveId(null);
                setDetail(null);
                setError('');
                void openSession(item, user).catch((err) => setError(err.message));
              }}
              title={item.title ?? '新对话'}
            >
              <MessageSquare size={14} />
              <span>{item.title || '新对话'}</span>
            </button>
          ))}
        </div>
        <footer className="sidebar-footer">
          <span className="environment-dot" />
          <span>本地模拟环境</span>
          <ShieldCheck size={14} />
        </footer>
      </aside>
      <main className="workspace">
        {user.model_mode === 'test' && (
          <div className="test-banner">程序测试模式 · 用于验证界面与流程，非真实模型评测</div>
        )}
        <header className="topbar">
          <div className="topbar-left">
            <button
              className="icon-button mobile-menu"
              aria-label="打开导航"
              onClick={() => setNavigationOpen(true)}
            >
              <Menu size={20} />
            </button>
            <Building2 size={18} />
            <label className="select-label">
              <span className="sr-only">当前企业</span>
              <select
                value={company ?? ''}
                onChange={(event) => void chooseCompany(event.target.value)}
              >
                <option value="">选择企业</option>
                {user.companies.map((c) => (
                  <option key={c.company_id} value={c.company_id}>
                    {companyName(c.company_id)}
                  </option>
                ))}
              </select>
              <ChevronDown size={13} />
            </label>
            <span className={`permission-label ${canUpdate ? '' : 'readonly'}`}>
              {company ? (canUpdate ? '可查询与补充' : '只读') : '企业待确认'}
            </span>
          </div>
          <div className="topbar-right">
            <span className={`connection-status ${user.model_ready ? 'connected' : ''}`}>
              <span />
              {user.model_mode === 'test'
                ? '程序测试模型'
                : user.model_ready
                  ? '模型已连接'
                  : '模型待连接'}
            </span>
            <label className="user-switch">
              <span className="user-avatar">{user.user_id === 'U001' ? '一' : '二'}</span>
              <span className="sr-only">当前模拟用户</span>
              <select value={user.user_id} onChange={(event) => void login(event.target.value)}>
                <option value="U001">测试用户一</option>
                <option value="U002">测试用户二</option>
              </select>
              <ChevronDown size={13} />
            </label>
          </div>
        </header>
        {error && (
          <div className="global-note" role="alert">
            <CircleAlert size={16} />
            <span>{error}</span>
            <button className="icon-button" aria-label="关闭提示" onClick={() => setError('')}>
              <X size={15} />
            </button>
          </div>
        )}
        {page === 'memory' ? (
          <div className="page-scroll">
            <MemoryView
              key={`${user.user_id}/${company}`}
              user={user}
              company={company}
              onDetail={setDetail}
              onDraft={prepareDraft}
              onChanged={changed}
            />
          </div>
        ) : page === 'runs' ? (
          <div className="page-scroll">
            <RunsView key={`${user.user_id}/${company}`} company={company} onDetail={setDetail} />
          </div>
        ) : (
          <>
            {(currentTask || taskError) && (
              <div className="task-strip">
                {currentTask ? (
                  <>
                    <span className="task-strip-icon">
                      <ClipboardCheck size={16} />
                    </span>
                    <strong>{taskName(currentTask.intent)}</strong>
                    <span>
                      {companyName(currentTask.company_id)} · {currentTask.period ?? '月份待确认'}
                    </span>
                    <span className="task-strip-status">
                      {currentTask.unresolved_json.needs_refresh ||
                      currentTask.snapshot_current === false
                        ? '待重新检查'
                        : statusName(currentTask.status)}
                    </span>
                    <button onClick={() => setDetail({ kind: 'task', data: currentTask })}>
                      查看详情
                      <ChevronRight size={13} />
                    </button>
                  </>
                ) : (
                  <>
                    <CircleAlert size={15} />
                    <span>任务状态暂时不可用</span>
                    <button onClick={changed}>重试</button>
                  </>
                )}
              </div>
            )}
            <div
              className="conversation-scroll"
              ref={scrollArea}
              onScroll={() => {
                const area = scrollArea.current;
                if (area) {
                  stickToBottom.current =
                    area.scrollHeight - area.scrollTop - area.clientHeight < 90;
                  if (stickToBottom.current) setNewMessages(false);
                }
              }}
            >
              <div className="conversation-content">
                {turnList.length === 0 ? (
                  <section className="welcome">
                    <span className="welcome-mark">
                      <Layers3 size={29} />
                    </span>
                    <span className="eyebrow">财税连续办理助手</span>
                    <h1>
                      把资料理清，
                      <br />
                      让办理接得上。
                    </h1>
                    <p>
                      查询有依据，补充可核验。
                      <br className="mobile-break" />
                      下次回来，也能接着处理。
                    </p>
                    <div className="starter-actions">
                      {[
                        {
                          Icon: BookOpen,
                          title: '查询月度资料',
                          description: '发票金额与银行收支',
                          text: '查询当前企业的月度发票金额与银行收支',
                        },
                        {
                          Icon: ClipboardCheck,
                          title: '检查发票用途',
                          description: '按适用规则找出缺项',
                          text: '检查当前企业的发票用途填写情况',
                        },
                        {
                          Icon: History,
                          title: '继续上次任务',
                          description: '恢复进度，再核对当前资料',
                          text: '继续处理当前企业的未完成任务',
                        },
                      ].map(({ Icon, title, description, text }) => (
                        <button key={title} onClick={() => prepareDraft(text)}>
                          <span className="starter-icon">
                            <Icon size={20} />
                          </span>
                          <strong>{title}</strong>
                          <small>{description}</small>
                          <ArrowRight size={15} />
                        </button>
                      ))}
                    </div>
                  </section>
                ) : (
                  turnList.map((turn) => (
                    <article className="turn" key={turn.turn_id}>
                      <div className="user-message">{turn.text}</div>
                      <div className="assistant-message">
                        <span className="assistant-avatar">
                          <Layers3 size={16} />
                        </span>
                        <div className="assistant-content">
                          {turn.final_message ? (
                            <>
                              <div className="prose">
                                <ReactMarkdown>{turn.final_message}</ReactMarkdown>
                              </div>
                              {turn.final_cards?.map((card, index) => (
                                <Results
                                  key={`${card.kind}/${index}`}
                                  card={card}
                                  canUpdate={
                                    user.companies.find((c) => c.company_id === card.company_id)
                                      ?.can_update ?? false
                                  }
                                  onDraft={prepareDraft}
                                  onDetail={setDetail}
                                />
                              ))}
                              <div className="answer-footer">
                                <CheckCheck size={13} />
                                <span>{statusName(turn.status)}</span>
                                {turn.runs?.at(-1) && (
                                  <button
                                    onClick={() =>
                                      setDetail({ kind: 'run', id: turn.runs!.at(-1)!.run_id })
                                    }
                                  >
                                    查看办理记录
                                    <ChevronRight size={12} />
                                  </button>
                                )}
                              </div>
                            </>
                          ) : turn.error ? (
                            <div className="turn-error">
                              <CircleAlert size={17} />
                              <div>
                                <strong>本轮未能完成</strong>
                                <p>{turn.error.message}</p>
                                {turn.runs?.at(-1) && (
                                  <button
                                    className="text-button"
                                    onClick={() =>
                                      setDetail({ kind: 'run', id: turn.runs!.at(-1)!.run_id })
                                    }
                                  >
                                    核对已提交操作
                                    <ChevronRight size={13} />
                                  </button>
                                )}
                              </div>
                            </div>
                          ) : ['cancelled', 'interrupted'].includes(turn.status) ? (
                            <p className="muted">本轮已停止，已提交的资料仍可在记录中核对。</p>
                          ) : (
                            <div className="processing">
                              <LoaderCircle size={16} className="spinning" />
                              <span>{turn.turn_id === activeId ? stage : '等待处理'}</span>
                            </div>
                          )}
                        </div>
                      </div>
                    </article>
                  ))
                )}
              </div>
            </div>
            <div className="composer-area">
              {newMessages && (
                <button
                  className="new-message-pill"
                  onClick={() => {
                    stickToBottom.current = true;
                    scrollArea.current?.scrollTo({
                      top: scrollArea.current.scrollHeight,
                      behavior: 'smooth',
                    });
                    setNewMessages(false);
                  }}
                >
                  查看新消息
                  <ChevronDown size={13} />
                </button>
              )}
              {!user.model_ready && (
                <p className="model-notice">
                  <CircleAlert size={14} />
                  模型暂未连接，完成本地配置并重启服务后即可办理。
                </p>
              )}
              <form
                className="composer"
                onSubmit={(event) => {
                  event.preventDefault();
                  void send();
                }}
              >
                <textarea
                  ref={composer}
                  value={draft}
                  onChange={(event) => setDraft(event.target.value)}
                  onKeyDown={onKeyDown}
                  onCompositionStart={() => {
                    composing.current = true;
                  }}
                  onCompositionEnd={() => {
                    composing.current = false;
                  }}
                  placeholder="告诉我需要查询或补充什么…"
                  rows={2}
                  aria-label="输入办理需求"
                />
                <div className="composer-toolbar">
                  <span>
                    {draft.length > 12000
                      ? `${draft.length.toLocaleString()} / 16,000 字符`
                      : 'Enter 发送 · Shift + Enter 换行'}
                  </span>
                  {activeId ? (
                    <button
                      type="button"
                      className="stop-button"
                      aria-label="停止本轮处理"
                      onClick={stop}
                    >
                      <Square size={13} />
                      停止
                    </button>
                  ) : (
                    <button
                      className="send-button"
                      aria-label="发送消息"
                      disabled={
                        !session ||
                        sending ||
                        !draft.trim() ||
                        draft.length > 16000 ||
                        !user.model_ready
                      }
                    >
                      {sending ? (
                        <LoaderCircle size={18} className="spinning" />
                      ) : (
                        <ArrowUp size={20} />
                      )}
                    </button>
                  )}
                </div>
              </form>
              <p className="composer-footnote">
                <ShieldCheck size={12} />
                比赛模拟资料 · 保存状态以实际核验结果为准
              </p>
            </div>
          </>
        )}
      </main>
      {detail && (
        <Details
          key={
            detail.kind === 'run' || detail.kind === 'source'
              ? `${detail.kind}/${detail.id}`
              : detail.kind
          }
          detail={detail}
          onClose={() => setDetail(null)}
        />
      )}
    </div>
  );
}
