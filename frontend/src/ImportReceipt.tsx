import { CheckCheck, FileSpreadsheet, ChevronRight } from 'lucide-react';
import { companyName, dateTime } from './api';
import type { ImportBatch } from './types';

export const importStatus = (status: string) =>
  ({
    draft: '尚未预览',
    validation_failed: '需要处理问题',
    preview_ready: '等待确认',
    committed: '已追加',
    no_change: '本次无新增',
    cancelled: '已取消',
    expired: '预览已过期',
  })[status] ?? status;

export function ImportReceipt({ batch, onOpen }: { batch: ImportBatch; onOpen?: () => void }) {
  const committed = ['committed', 'no_change'].includes(batch.status);
  return (
    <section className={`result-card import-receipt ${committed ? 'verified' : ''}`}>
      <div className="result-range">
        <span>{companyName(batch.company_id)}</span>
        <span>发票数据导入</span>
        <time>{dateTime(batch.committed_at ?? batch.created_at)}</time>
      </div>
      <div className="receipt-heading">
        {committed ? <CheckCheck size={20} /> : <FileSpreadsheet size={20} />}
        <h3>{importStatus(batch.status)}</h3>
      </div>
      <p className="import-summary">
        {committed
          ? `本批次新增 ${batch.receipt?.inserted_count ?? 0} 张，跳过 ${batch.receipt?.skipped_count ?? 0} 行。`
          : `预览新增 ${batch.counts.new ?? 0} 张，尚未写入发票资料。`}
      </p>
      {committed && (
        <p className="muted small">导入结果以本批次回执为准；相关金额与用途缺项需要重新查询。</p>
      )}
      {onOpen && (
        <button className="text-button" onClick={onOpen}>
          查看批次与明细
          <ChevronRight size={14} />
        </button>
      )}
    </section>
  );
}
