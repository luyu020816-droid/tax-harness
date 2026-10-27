import { ImportReceipt } from './ImportReceipt';
import { ArrowUpRight, CheckCheck, ChevronRight, CircleAlert } from 'lucide-react';
import { companyName, dateTime, money } from './api';
import type { Detail, ResultCard, Rule } from './types';

function RuleLinks({ rules, onDetail }: { rules: Rule[]; onDetail: (detail: Detail) => void }) {
  return (
    <div className="rule-links">
      <span>依据</span>
      {rules.map((rule) => (
        <button key={rule.rule_version_id} onClick={() => onDetail({ kind: 'rule', data: rule })}>
          {rule.display_name} {rule.business_version} <ChevronRight size={12} />
        </button>
      ))}
    </div>
  );
}

export function Results({
  card,
  canUpdate,
  onDraft,
  onDetail,
}: {
  card: ResultCard;
  canUpdate: boolean;
  onDraft: (text: string) => void;
  onDetail: (detail: Detail) => void;
}) {
  if (card.kind === 'import_preview' || card.kind === 'import_receipt')
    return (
      <ImportReceipt batch={card} onOpen={() => onDetail({ kind: 'import', id: card.batch_id })} />
    );
  const range = (
    <div className="result-range">
      <span>{companyName(card.company_id)}</span>
      {card.period && (
        <>
          <span className="separator">/</span>
          <span>{card.period.replace('-', ' 年 ')} 月</span>
        </>
      )}
      {card.as_of && <time>{dateTime(card.as_of)} 查询</time>}
    </div>
  );
  if (card.kind === 'monthly_summary')
    return (
      <section className="result-card summary-card">
        {range}
        <div className="metrics">
          {[
            {
              label: '发票总额',
              value: card.invoice_total,
              count: card.invoice_count,
              unit: '张发票',
            },
            {
              label: '银行收入',
              value: card.bank_income,
              count: card.income_count,
              unit: '笔收入',
            },
            {
              label: '银行支出',
              value: card.bank_expense,
              count: card.expense_count,
              unit: '笔支出',
            },
          ].map(({ label, value, count, unit }) => (
            <div className="metric" key={label}>
              <span className="metric-label">{label}</span>
              <strong>
                {money(value)}
                <small>元</small>
              </strong>
              <span className="metric-count">
                {count} {unit}
              </span>
            </div>
          ))}
        </div>
        {!card.has_records && <p className="muted inline-note">该范围查询成功，没有记录。</p>}
        <RuleLinks rules={card.rule_refs ?? []} onDetail={onDetail} />
      </section>
    );
  if (card.kind === 'purpose_check' || card.kind === 'invoices')
    return (
      <section className="result-card list-card">
        {range}
        <div className="result-title">
          <div>
            <h3>
              {card.kind === 'purpose_check' ? (
                card.missing_count ? (
                  <>
                    待补 <em>{card.missing_count}</em> 张发票用途
                  </>
                ) : (
                  '必填用途已完整'
                )
              ) : (
                `找到 ${card.total} 张发票`
              )}
            </h3>
            <p>
              {card.kind === 'purpose_check'
                ? '按本企业适用规则检查当前保存资料'
                : '票面项目仅用于定位，实际用途以用户确认为准'}
            </p>
          </div>
        </div>
        {card.rows.length > 0 && (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>序号 / 发票</th>
                  <th>开票日期</th>
                  <th className="number">金额（元）</th>
                  <th>用途</th>
                  <th>
                    <span className="sr-only">操作</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {card.rows.map((row, index) => (
                  <tr key={`${row.company_id}/${row.invoice_id}`}>
                    <td>
                      <span className="row-index">{index + 1}</span>
                      <strong>{row.invoice_id}</strong>
                    </td>
                    <td>{row.invoice_date}</td>
                    <td className="number">{money(row.amount)}</td>
                    <td>
                      {row.purpose?.trim() ? (
                        row.purpose
                      ) : (
                        <span className="badge waiting">待补用途</span>
                      )}
                    </td>
                    <td>
                      <button
                        className="text-button"
                        disabled={!canUpdate}
                        title={canUpdate ? '填写实际用途后发送' : '当前用户只有查询权限'}
                        onClick={() =>
                          onDraft(`把${companyName(row.company_id)} ${row.invoice_id} 的用途补为`)
                        }
                      >
                        补充用途
                        <ArrowUpRight size={13} />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <div className="result-footnote">
          <span>
            {card.total === 0
              ? card.kind === 'purpose_check'
                ? '本次无必填缺项'
                : '本次未找到发票'
              : `当前展示 ${card.offset + 1}—${card.offset + card.rows.length} 条，共 ${card.total} 条`}
          </span>
          {card.has_more && (
            <button
              className="text-button"
              onClick={() =>
                onDraft(
                  `继续展示${companyName(card.company_id)} ${card.period ?? ''} 上一份清单的下一页`
                )
              }
            >
              下一页
              <ChevronRight size={14} />
            </button>
          )}
        </div>
        <RuleLinks rules={card.rule_refs ?? []} onDetail={onDetail} />
      </section>
    );
  if (card.kind === 'operation') {
    const verified = card.verification_state === 'verified';
    const rejected = card.commit_state === 'rejected';
    const label = rejected
      ? '本项未保存'
      : card.verification_state === 'changed_since_commit'
        ? '本次曾提交，当前值已有变化'
        : verified
          ? card.commit_state === 'no_change'
            ? '当前已经是该值，已核验'
            : '已保存并核验'
          : '已提交，核验待完成';
    return (
      <section className={`result-card receipt-card ${verified ? 'verified' : 'unverified'}`}>
        {range}
        <div className="receipt-heading">
          {verified ? <CheckCheck size={20} /> : <CircleAlert size={20} />}
          <h3>{label}</h3>
          <span>{card.invoice_id}</span>
        </div>
        <dl className="saved-values">
          {Object.entries(card.saved_values ?? card.patch).map(([key, value]) => (
            <div key={key}>
              <dt>{key === 'purpose' ? '用途' : '备注'}</dt>
              <dd>{value?.trim() ? value : '已清空'}</dd>
            </div>
          ))}
        </dl>
      </section>
    );
  }
  if (card.kind === 'rules')
    return (
      <section className="result-card">
        {range}
        <h3>适用规则</h3>
        <RuleLinks rules={card.rules} onDetail={onDetail} />
      </section>
    );
  return null;
}
