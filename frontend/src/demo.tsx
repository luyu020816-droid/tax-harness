import { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import {
  Activity,
  ArrowDown,
  ArrowRight,
  ArrowUpRight,
  Check,
  CheckCheck,
  ChevronDown,
  ChevronRight,
  Database,
  FileCheck2,
  FileText,
  Fingerprint,
  History,
  Layers3,
  MessageSquare,
  MoreHorizontal,
  Paperclip,
  Plus,
  ShieldCheck,
  X,
  Zap,
} from 'lucide-react';
import './demo.css';

const invoices = [
  {
    id: 'F002',
    name: '技术服务费',
    vendor: '远山信息技术有限公司',
    amount: '12,800.00',
    field: '用途',
    value: '研发项目技术服务',
  },
  {
    id: 'F006',
    name: '办公设备',
    vendor: '新禾办公设备有限公司',
    amount: '6,420.00',
    field: '备注',
    value: '行政办公设备采购',
  },
  {
    id: 'F009',
    name: '差旅住宿费',
    vendor: '南城酒店管理有限公司',
    amount: '1,860.00',
    field: '用途',
    value: '客户现场项目差旅',
  },
];
function Demo() {
  const [scenario, setScenario] = useState<'review' | 'recovery'>('review');
  const [phase, setPhase] = useState(0);
  const [drawer, setDrawer] = useState<'source' | 'memory' | null>(null);
  const [draft, setDraft] = useState('');
  const [message, setMessage] = useState('');
  const [selected, setSelected] = useState('F002');
  const [saved, setSaved] = useState(false);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);
  useEffect(
    () => () => {
      if (timer.current) clearInterval(timer.current);
    },
    []
  );
  function switchScenario(next: 'review' | 'recovery') {
    if (timer.current) clearInterval(timer.current);
    setScenario(next);
    setPhase(0);
    setSaved(false);
    setMessage('');
  }
  function recover() {
    setPhase(1);
    timer.current = setInterval(
      () =>
        setPhase((p) => {
          if (p >= 3) {
            if (timer.current) clearInterval(timer.current);
            return 4;
          }
          return p + 1;
        }),
      850
    );
  }
  function send() {
    if (!draft.trim()) return;
    setMessage(draft.trim());
    setDraft('');
  }
  const current = invoices.find((i) => i.id === selected)!;
  return (
    <div className="demo-shell">
      <aside className="sidebar">
        <a className="brand" href="/demo.html">
          <span className="brand-icon">
            <Layers3 size={22} />
          </span>
          云帐房<span className="brand-plus">LAB</span>
        </a>
        <div className="workspace">
          <span className="company-avatar">甲</span>
          <div>
            <strong>甲企业工作空间</strong>
            <small>财税连续办理助手</small>
          </div>
          <ChevronDown size={14} />
        </div>
        <button
          className="new-task"
          onClick={() => {
            switchScenario('review');
            setSelected('F002');
          }}
        >
          <Plus size={17} /> 开始新的办理 <span>⌘ K</span>
        </button>
        <div className="nav-label">工作空间</div>
        <button className="nav active" onClick={() => setDrawer(null)}>
          <MessageSquare size={18} />
          办理工作台
          <span className="nav-dot" />
        </button>
        <button className="nav" onClick={() => setDrawer('memory')}>
          <Fingerprint size={18} />
          记忆与偏好<small>03</small>
        </button>
        <button className="nav" onClick={() => setDrawer('source')}>
          <FileText size={18} />
          知识与规则
          <ArrowUpRight size={13} />
        </button>
        <button className="nav" onClick={() => switchScenario('recovery')}>
          <Activity size={18} />
          运行与恢复
        </button>
        <div className="nav-label recent-label">
          最近办理 <History size={13} />
        </div>
        <button
          className={'recent ' + (scenario === 'review' ? 'chosen' : '')}
          onClick={() => switchScenario('review')}
        >
          <span />
          10 月发票资料检查<small>进行中</small>
        </button>
        <button
          className={'recent ' + (scenario === 'recovery' ? 'chosen' : '')}
          onClick={() => switchScenario('recovery')}
        >
          <span />
          中断后的安全续办
        </button>
        <div className="sidebar-note">
          <ShieldCheck size={19} />
          <strong>每一步，都有据可查。</strong>
          <p>
            原话、规则、操作回执
            <br />
            串起完整办理过程。
          </p>
          <span>
            TRACEABLE BY DESIGN <ArrowUpRight size={12} />
          </span>
        </div>
        <div className="profile">
          <span className="profile-avatar">林</span>
          <div>
            <strong>林会计</strong>
            <small>模拟身份 · U001</small>
          </div>
          <MoreHorizontal size={19} />
        </div>
      </aside>
      <div className="main-shell">
        <header className="topbar">
          <div>
            工作空间 <ChevronRight size={13} />
            <strong>办理工作台</strong>
          </div>
          <div>
            <span className="demo-badge">交互概念示例 · 模拟数据</span>
            <span className="local-indicator" />
            本地演示
          </div>
        </header>
        <main>
          <div className="page-heading">
            <div>
              <div className="eyebrow">YOUR FINANCE, IN FLOW</div>
              <h1>让每一笔，接得上。</h1>
              <p>从资料检查到结果核验，让办理过程清晰可见。</p>
            </div>
            <button className="outline-button" onClick={() => setDrawer('source')}>
              <History size={15} />
              查看办理依据
              <ArrowUpRight size={14} />
            </button>
          </div>
          <div className="overview">
            <div className="overview-title">
              <span className="overview-icon">
                <FileCheck2 size={23} />
              </span>
              <div>
                <small>当前办理</small>
                <strong>2026 年 10 月 · 发票资料检查</strong>
              </div>
            </div>
            <div className="metric">
              <small>已检查发票</small>
              <strong>
                24 <span>张</span>
              </strong>
            </div>
            <div className="metric">
              <small>待补充资料</small>
              <strong className="orange">
                {saved ? '2' : '3'} <span>项</span>
              </strong>
            </div>
            <div className="progress-metric">
              <small>
                资料完整度 <b>{saved ? '92' : '88'}%</b>
              </small>
              <div className="progress-track">
                <i style={{ width: saved ? '92%' : '88%' }} />
              </div>
              <span>演示统计 · 非业务查询结果</span>
            </div>
          </div>
          <div className="content-grid">
            <section className="conversation">
              <div className="section-top">
                <span>
                  <span className="live-dot" />
                  连续办理
                </span>
                <div className="scenario-switch">
                  <button
                    className={scenario === 'review' ? 'selected' : ''}
                    onClick={() => switchScenario('review')}
                  >
                    资料检查
                  </button>
                  <button
                    className={scenario === 'recovery' ? 'selected' : ''}
                    onClick={() => switchScenario('recovery')}
                  >
                    故障恢复
                  </button>
                </div>
              </div>
              <div className="chat-scroll">
                <div className="date-separator">
                  <span />
                  今天 · 演示会话
                  <span />
                </div>
                <div className="user-message">
                  <div>
                    <small>
                      林会计 <span>10:24</span>
                    </small>
                    <p>
                      {scenario === 'review'
                        ? '帮我检查甲企业 10 月发票，看看还有哪些资料需要补充。记住，只补用途和备注，不修改金额。'
                        : '刚才保存 F002 时连接中断了，帮我核对是否保存成功，再继续办理。'}
                    </p>
                  </div>
                  <span className="user-avatar">林</span>
                </div>
                <div className="assistant-message">
                  <span className="assistant-avatar">
                    <Layers3 size={20} />
                  </span>
                  <div className="answer-body">
                    <small>
                      云帐房助手 <span>可追溯的办理过程</span>
                    </small>
                    <button className="memory-strip" onClick={() => setDrawer('memory')}>
                      <Fingerprint size={14} />
                      <span>已保留你的原话约束：只补用途和备注，不修改金额</span>
                      <ChevronRight size={13} />
                    </button>
                    {scenario === 'review' ? (
                      <>
                        <h2>检查完成，有 3 项资料需要你确认。</h2>
                        <p className="answer-copy">
                          已按甲企业当前适用规则检查 24 张发票。你可以逐项补充，
                          <br className="wide-break" />
                          确认后再保存。
                          <button className="citation" onClick={() => setDrawer('source')}>
                            规则依据 ↗
                          </button>
                        </p>
                        <div className="invoice-table">
                          <div className="table-caption">
                            <span>待补充清单</span>
                            <span>3 ITEMS</span>
                          </div>
                          <div className="table-head">
                            <span>发票 / 项目</span>
                            <span>价税合计（元）</span>
                            <span>待补内容</span>
                          </div>
                          {invoices.map((i) => (
                            <button
                              className={'invoice-row ' + (selected === i.id ? 'row-selected' : '')}
                              key={i.id}
                              onClick={() => {
                                setSelected(i.id);
                                setSaved(false);
                              }}
                            >
                              <span>
                                <strong>
                                  {i.id}
                                  <span>{i.name}</span>
                                </strong>
                                <small>{i.vendor}</small>
                              </span>
                              <span className="amount">{i.amount}</span>
                              <span>
                                <b className="field-tag">{i.field}</b>
                                <ChevronRight size={13} />
                              </span>
                            </button>
                          ))}
                        </div>
                        <div className="edit-card">
                          <div>
                            <span className="edit-label">
                              {current.id} · {current.field}建议
                            </span>
                            <span className="pending-tag">{saved ? '演示已确认' : '待你确认'}</span>
                          </div>
                          <p>{current.value}</p>
                          <footer>
                            <span>
                              <ShieldCheck size={13} />
                              示例交互，不写入业务数据
                            </span>
                            <button disabled={saved} onClick={() => setSaved(true)}>
                              {saved ? <CheckCheck size={14} /> : <Check size={14} />}{' '}
                              {saved ? '演示保存完成' : '确认此项 · 演示'}
                            </button>
                          </footer>
                        </div>
                      </>
                    ) : (
                      <>
                        <h2>
                          {phase === 4
                            ? '已找回原回执，可以继续办理。'
                            : '连接中断，办理记录仍然保留。'}
                        </h2>
                        <p className="answer-copy">
                          恢复时先核对上次保存结果，再继续处理剩余事项。
                        </p>
                        <div className={'recovery-card ' + (phase === 4 ? 'complete' : '')}>
                          <span className="recovery-symbol">
                            {phase === 4 ? <CheckCheck size={28} /> : <History size={28} />}
                          </span>
                          <h3>
                            {phase === 4
                              ? 'F002 已保存 · 未重复执行'
                              : phase > 0
                                ? '正在核对原请求…'
                                : '上一次连接已中断'}
                          </h3>
                          <p>
                            {phase === 4
                              ? '示例回执 OP-1024 · 发票修订 1 → 2'
                              : '原请求、执行记录和已有回执可用于安全续办'}
                          </p>
                          <button
                            disabled={phase > 0 && phase < 4}
                            onClick={phase === 4 ? () => switchScenario('review') : recover}
                          >
                            {phase === 4
                              ? '继续检查剩余资料'
                              : phase > 0
                                ? '核验中…'
                                : '核对结果并继续'}
                            <ArrowRight size={15} />
                          </button>
                        </div>
                        <div className="recovery-note">
                          <ShieldCheck size={15} />
                          此处为恢复流程动画，不会调用后端或执行真实保存。
                        </div>
                      </>
                    )}
                    <div className="answer-footer">
                      <span>
                        <CheckCheck size={13} />{' '}
                        {scenario === 'review'
                          ? '当前企业与规则已读取 · 演示'
                          : '原请求身份保留 · 演示'}
                      </span>
                      <button onClick={() => setDrawer('source')}>
                        查看证据链 <ArrowUpRight size={12} />
                      </button>
                    </div>
                  </div>
                </div>
                {message && (
                  <div className="demo-response">
                    <strong>已接收演示输入：{message}</strong>
                    <p>这是视觉原型，未连接模型。可切换“故障恢复”，体验回执核验流程。</p>
                  </div>
                )}
              </div>
              <div className="composer-wrap">
                <div className="quick-actions">
                  <button onClick={() => setDrawer('memory')}>
                    <Fingerprint size={12} />
                    这次记住了什么
                  </button>
                  <button onClick={() => switchScenario('recovery')}>
                    <Zap size={12} />
                    体验中断恢复
                  </button>
                </div>
                <form
                  className="composer"
                  onSubmit={(e) => {
                    e.preventDefault();
                    send();
                  }}
                >
                  <input
                    aria-label="演示消息"
                    placeholder="继续交代，让办理接着往下走…"
                    value={draft}
                    onChange={(e) => setDraft(e.target.value)}
                  />
                  <div>
                    <span>
                      <Paperclip size={16} />
                      <span>演示输入 · 不发送到模型</span>
                    </span>
                    <button type="submit" aria-label="发送演示消息" disabled={!draft.trim()}>
                      <ArrowRight size={19} />
                    </button>
                  </div>
                </form>
                <div className="composer-caption">
                  <ShieldCheck size={11} />
                  每次修改前由你确认 <span>界面概念基于 85290d2</span>
                </div>
              </div>
            </section>
            <aside className="inspector">
              <div className="inspector-heading">
                <span>
                  <Activity size={16} />
                  办理轨迹
                </span>
                <span className="outline-tag">LIVE DEMO</span>
              </div>
              <h3>看见每一步的依据</h3>
              <p className="inspector-intro">结果背后，是可核对的执行过程。</p>
              <div className="timeline">
                {[
                  ['读取当前状态', '甲企业 · 2026 年 10 月', Database],
                  ['找回办理约束', '用户原话保护 · 来源有效', Fingerprint],
                  [
                    scenario === 'recovery' ? '核对原操作回执' : '应用当前规则',
                    scenario === 'recovery'
                      ? phase >= 2
                        ? '已找到 OP-1024 · 演示'
                        : '等待开始恢复核验'
                      : '发票资料规则 · 当前版本',
                    ShieldCheck,
                  ],
                  [
                    scenario === 'recovery' ? '继续未完成事项' : '生成补充清单',
                    scenario === 'recovery'
                      ? phase === 4
                        ? '已恢复 · 不重复保存'
                        : '核验后再继续'
                      : '3 项待补充 · 等待确认',
                    FileCheck2,
                  ],
                ].map(([title, detail, Icon], n) => {
                  const Symbol = Icon as typeof Database;
                  const done =
                    scenario === 'review' ? n < 3 : n < 2 || (n === 2 && phase >= 2) || phase === 4;
                  return (
                    <div className={'timeline-step ' + (done ? 'done' : 'waiting')} key={n}>
                      <span className="step-dot">
                        {done ? <Check size={13} /> : <Symbol size={13} />}
                      </span>
                      <div>
                        <strong>{title as string}</strong>
                        <small>{detail as string}</small>
                        {n === 1 && (
                          <button onClick={() => setDrawer('memory')}>
                            查看保留原话 <ArrowUpRight size={11} />
                          </button>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
              <div className="context-card">
                <div>
                  <Layers3 size={15} />
                  <strong>长对话，也有连续性</strong>
                </div>
                <p>
                  摘要帮助导航，原话保留依据。
                  <br />
                  需要时，回到来源核对。
                </p>
                <div className="context-visual">
                  <span />
                  <span />
                  <span />
                  <span />
                  <span />
                  <span />
                  <span />
                  <span />
                  <i>
                    <ArrowDown size={13} />
                  </i>
                  <b>原话保护</b>
                </div>
                <button onClick={() => setDrawer('memory')}>
                  查看记忆分层
                  <ArrowRight size={13} />
                </button>
              </div>
              <div className="boundary-note">
                <ShieldCheck size={15} />
                <div>
                  <strong>你的确认，是保存的前提</strong>
                  <p>当前画面仅演示操作，不产生真实回执。</p>
                </div>
              </div>
              <div className="inspector-bottom">
                <span className="live-dot" />
                交互原型 <span>DESIGN PREVIEW / 01</span>
              </div>
            </aside>
          </div>
        </main>
      </div>
      {drawer && (
        <div className="drawer-overlay" onClick={() => setDrawer(null)}>
          <section
            className="drawer"
            role="dialog"
            aria-modal="true"
            aria-label={drawer === 'memory' ? '记忆与原话' : '办理依据'}
            onClick={(e) => e.stopPropagation()}
            onKeyDown={(e) => {
              if (e.key === 'Escape') setDrawer(null);
            }}
            tabIndex={-1}
            ref={(el) => el?.focus()}
          >
            <button className="close-drawer" aria-label="关闭" onClick={() => setDrawer(null)}>
              <X size={20} />
            </button>
            <div className="eyebrow">
              {drawer === 'memory' ? 'MEMORY WITH EVIDENCE' : 'SOURCE OF TRUTH'}
            </div>
            <h2>{drawer === 'memory' ? '记住的，能回到原话。' : '每个结论，都有来处。'}</h2>
            <p className="drawer-intro">基于本次提交能力设计的交互示例。以下均为模拟内容。</p>
            {drawer === 'memory' ? (
              <>
                <div className="source-block">
                  <span>01 / 用户原话 · 受保护摘录</span>
                  <blockquote>“只补用途和备注，不修改金额。”</blockquote>
                  <small>来源：本次演示会话 · 甲企业 · 当前有效</small>
                </div>
                <div className="source-block">
                  <span>02 / 导航摘要 · 帮助查找</span>
                  <p>正在办理甲企业 10 月发票资料补充，有 3 项待确认。摘要不能替代当前业务查询。</p>
                </div>
                <div className="source-block">
                  <span>03 / 正式状态 · 重新读取</span>
                  <p>企业、规则版本和票据修订应在实际办理时核对。历史记忆不能授权写入。</p>
                </div>
              </>
            ) : (
              <>
                <div className="source-block">
                  <span>规则依据 / 模拟条款</span>
                  <h3>发票资料完整性检查</h3>
                  <p>
                    演示条款：按当前企业适用规则检查发票用途与备注。实际条件须由后端当前有效规则提供。
                  </p>
                  <small>用途：界面展示 · 不作为财税建议</small>
                </div>
                <div className="source-block">
                  <span>数据来源 / 模拟快照</span>
                  <p>24 张发票、3 项待补充和所有金额均为界面样例，未读取业务数据库。</p>
                </div>
                <div className="source-block">
                  <span>对应代码能力 / 85290d2</span>
                  <p>
                    摘要原话证据保护、状态变化后读取正式状态、失败轮次重试前回读操作回执、三策略可控对比。
                  </p>
                </div>
              </>
            )}
            <button className="drawer-back" onClick={() => setDrawer(null)}>
              回到办理工作台
              <ArrowRight size={16} />
            </button>
          </section>
        </div>
      )}
    </div>
  );
}
createRoot(document.getElementById('root')!).render(<Demo />);
