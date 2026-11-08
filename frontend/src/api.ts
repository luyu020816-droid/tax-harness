import type { Turn } from './types';

export function turnStatusName(turn: Turn): string {
  if (turn.status === 'failed' && turn.final_cards.some((card) => card.kind === 'operation'))
    return '保存回执已交付，后续未完成';
  const items = turn.final_cards.flatMap((card) =>
    card.kind === 'request_progress' ? card.items : []
  );
  if (turn.status === 'completed' && items.some((item) => item.status !== 'verified'))
    return items.some((item) => item.status === 'blocked') ? '已反馈，含未完成事项' : '等待补充';
  return statusName(turn.status);
}

export class ApiError extends Error {
  constructor(
    public code: string,
    message: string,
    public status: number
  ) {
    super(message);
  }
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch('/api/v1' + path, {
      credentials: 'same-origin',
      ...options,
      headers: {
        ...(options.body && !(options.body instanceof FormData)
          ? { 'Content-Type': 'application/json' }
          : {}),
        ...options.headers,
      },
    });
  } catch (error) {
    if (options.signal?.aborted) throw error;
    throw new ApiError(
      'NETWORK_UNAVAILABLE',
      '连接中断，暂时无法确认服务端处理状态。请检查网络和本地服务；涉及保存时，先核对办理记录中的实际回执。',
      0
    );
  }
  if (response.status === 204) return undefined as T;
  let data;
  try {
    data = await response.json();
  } catch {
    throw new ApiError(
      'SERVICE_UNAVAILABLE',
      response.ok
        ? '服务返回了无法读取的结果，处理状态暂时无法确认；涉及保存时，请先核对实际回执。'
        : `业务服务返回异常（HTTP ${response.status}），暂时无法确认处理状态；涉及保存时，请先核对实际回执。`,
      response.status
    );
  }
  if (!response.ok) {
    if (!data || typeof data !== 'object' || Array.isArray(data))
      throw new ApiError(
        'SERVICE_UNAVAILABLE',
        '业务服务返回了无效响应，暂时无法确认处理状态；请先核对实际回执。',
        response.status
      );
    throw new ApiError(
      typeof data.code === 'string' ? data.code : 'REQUEST_FAILED',
      typeof data.message === 'string' ? data.message : '请求未能完成，请检查当前状态后重试。',
      response.status
    );
  }
  if (data === null || typeof data !== 'object')
    throw new ApiError(
      'INVALID_RESPONSE',
      '服务返回了无效结果，暂时无法确认处理状态；请先核对实际回执。',
      response.status
    );
  return data as T;
}

export const body = (value: unknown) => JSON.stringify(value);
export const requestId = () => crypto.randomUUID();
export const companyName = (id: string) =>
  id === 'C001' ? '甲企业' : id === 'C002' ? '乙企业' : id;
export const money = (value: string) => {
  if (!/^-?\d+(\.\d{1,2})?$/.test(value)) return '金额格式异常';
  const [whole, fraction = '00'] = value.split('.');
  return whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',') + '.' + fraction.padEnd(2, '0');
};
export const dateTime = (value: string) =>
  new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(new Date(value));
export const taskName = (intent: string) =>
  ({ query_month: '月度资料查询', explain_rules: '规则查询', complete_purposes: '发票用途补全' })[
    intent
  ] ?? intent;
export const statusName = (status: string) =>
  ({
    active: '办理中',
    running: '处理中',
    queued: '排队中',
    waiting_user: '等待补充',
    completed: '已完成',
    cancelled: '已取消',
    replaced: '已替换',
    deleted: '已删除',
    resolved: '已解决',
    failed: '未完成',
    interrupted: '已停止',
  })[status] ?? status;
