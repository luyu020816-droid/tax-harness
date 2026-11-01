import {
  ArrowUpRight,
  BookOpen,
  CheckCheck,
  CircleDot,
  FileCheck2,
  History,
  Layers3,
  MessageSquare,
  ShieldCheck,
  Sparkles,
} from 'lucide-react';
import { companyName, statusName, taskName } from './api';
import type { Detail, Task, Turn } from './types';

export function OrbitWelcome({ onDraft }: { onDraft: (text: string) => void }) {
  return (
    <section className="orbit-welcome">
      <div className="orbit-welcome-top">
        <span className="orbit-kicker">
          <i /> INTELLIGENCE. UNDER CONTROL.
        </span>
        <span className="orbit-coordinate">WORKSPACE / 001</span>
      </div>
      <div className="orbit-intro">
        <div>
          <span className="orbit-intro-label">你的财税工作，从一句话开始。</span>
          <h1>
            让复杂财税，
            <br />
            <span>进入智控时代。</span>
          </h1>
          <p>
            直接说出需求，查询资料、核对票据、继续办理。
            <br />
            每一步执行，都有依据可循。
          </p>
        </div>
        <div className="orbit-mini-scene" aria-hidden="true">
          <div className="orbit-mini-ring" />
          <div className="orbit-mini-ring second" />
          <div className="orbit-mini-globe">
            <Layers3 size={39} strokeWidth={1.2} />
          </div>
          <span className="orbit-signal signal-one" />
          <span className="orbit-signal signal-two" />
          <span className="orbit-scene-label">HUMAN IN CONTROL</span>
        </div>
      </div>
      <div className="orbit-capabilities">
        <span>
          <MessageSquare size={13} /> 连续对话
        </span>
        <span>
          <BookOpen size={13} /> 证据追溯
        </span>
        <span>
          <ShieldCheck size={13} /> 受控操作
        </span>
      </div>
      <div className="orbit-prompt-heading">
        <Sparkles size={14} /> 从这些任务开始 <span>点击后可编辑，再发送</span>
      </div>
      <div className="orbit-starters">
        {[
          {
            Icon: FileCheck2,
            title: '检查发票用途',
            description: '定位待补字段，查看检查依据',
            text: '检查当前企业2026年9月的发票用途填写情况，列出待补项和依据',
          },
          {
            Icon: CircleDot,
            title: '查询月度资料',
            description: '汇总发票与银行收支',
            text: '查询当前企业2026年9月的月度发票金额与银行收支',
          },
          {
            Icon: History,
            title: '继续上次任务',
            description: '核对当前状态，接着办理',
            text: '继续处理当前企业的未完成任务，先核对最新状态',
          },
        ].map(({ Icon, title, description, text }) => (
          <button key={title} onClick={() => onDraft(text)}>
            <Icon size={18} />
            <strong>{title}</strong>
            <small>{description}</small>
            <ArrowUpRight size={14} />
          </button>
        ))}
      </div>
    </section>
  );
}

export function OrbitContext({
  company,
  task,
  taskError,
  turns,
  active,
  stage,
  modelReady,
  onDetail,
}: {
  company: string | null;
  task: Task | null;
  taskError: string;
  turns: Turn[];
  active: boolean;
  stage: string;
  modelReady: boolean;
  onDetail: (detail: Detail) => void;
}) {
  const latest = turns.at(-1);
  const run = latest?.runs?.at(-1);
  const rules =
    latest?.final_cards.flatMap((card) =>
      card.kind === 'rules' ? card.rules : 'rule_refs' in card ? (card.rule_refs ?? []) : []
    ) ?? [];
  const distinctRules = [...new Map(rules.map((rule) => [rule.rule_version_id, rule])).values()];
  return (
    <aside className="orbit-context" aria-label="当前办理上下文">
      <div className="orbit-context-title">
        <span>LIVE CONTEXT</span>
        <ShieldCheck size={16} />
      </div>
      <section className="orbit-context-block">
        <span className="orbit-context-label">当前办理范围</span>
        <h2>{company ? companyName(company) : '企业待确认'}</h2>
        <p>
          仅使用当前身份可访问的企业资料。
          <br />
          切换企业后，将核对对应任务状态。
        </p>
        <span className="orbit-context-badge">
          <ShieldCheck size={12} /> 企业范围隔离
        </span>
      </section>
      <section className="orbit-context-block">
        <span className="orbit-context-label">任务与执行</span>
        <div className={`orbit-execution ${active ? 'is-running' : ''}`} role="status">
          <i />
          {active
            ? stage
            : !modelReady
              ? '模型待连接'
              : latest
                ? statusName(latest.status)
                : '等待你的第一条指令'}
        </div>
        {task ? (
          <>
            <h3>{taskName(task.intent)}</h3>
            <p>
              {task.period ?? '月份待确认'} ·{' '}
              {task.snapshot_current === false || task.unresolved_json.needs_refresh
                ? '待重新检查'
                : statusName(task.status)}
            </p>
            <button
              className="orbit-context-link"
              onClick={() => onDetail({ kind: 'task', data: task })}
            >
              查看任务详情 <ArrowUpRight size={13} />
            </button>
          </>
        ) : (
          <p>
            {taskError
              ? '任务状态暂不可用，可在上方重试。'
              : '发送需求后，这里会显示服务返回的任务状态。'}
          </p>
        )}
        {run && (
          <button
            className="orbit-context-link"
            onClick={() => onDetail({ kind: 'run', id: run.run_id })}
          >
            <History size={13} /> 查看本轮办理记录 <ArrowUpRight size={13} />
          </button>
        )}
      </section>
      <section className="orbit-context-block">
        <span className="orbit-context-label">EVIDENCE / 依据</span>
        {distinctRules.length ? (
          <div className="orbit-rule-list">
            {distinctRules.map((rule) => (
              <button
                key={rule.rule_version_id}
                onClick={() => onDetail({ kind: 'rule', data: rule })}
              >
                <BookOpen size={14} />
                <span>
                  {rule.display_name}
                  <small>{rule.business_version}</small>
                </span>
                <ArrowUpRight size={12} />
              </button>
            ))}
          </div>
        ) : (
          <>
            <div className="orbit-evidence-placeholder">
              <BookOpen size={22} strokeWidth={1.2} />
              <span>依据随结果呈现</span>
            </div>
            <p>查询结果、知识引文和保存回执会出现在对话中，可点击查看原始依据。</p>
          </>
        )}
      </section>
      <div className="orbit-context-foot">
        <CheckCheck size={15} />
        <div>
          你掌握最后的决定<small>保存状态以实际核验结果为准</small>
        </div>
      </div>
    </aside>
  );
}
