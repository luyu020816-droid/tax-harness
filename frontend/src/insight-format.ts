import type { InsightCost } from './insight-types';
export const duration = (ms: number | null | undefined) =>
  ms == null
    ? '未采集'
    : ms > 0 && ms < 0.1
      ? '<0.1 ms'
      : ms < 1000
        ? `${ms.toFixed(1)} ms`
        : `${(ms / 1000).toFixed(2)} 秒`;
export const number = (n: number) => n.toLocaleString('zh-CN');
export function fee(cost?: InsightCost) {
  if (!cost) return '未采集';
  if (cost.complete_cost == null)
    return cost.currency && cost.known_cost
      ? `${cost.currency} ${cost.known_cost} + 未知`
      : '待计价';
  return cost.currency
    ? `${cost.currency} ${Number(cost.complete_cost) > 0 && Number(cost.complete_cost) < 0.000001 ? '<0.000001（非零）' : Number(cost.complete_cost).toFixed(6)}`
    : '暂无计费调用';
}
