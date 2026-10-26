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
  const response = await fetch('/api/v1' + path, {
    credentials: 'same-origin',
    ...options,
    headers: {
      ...(options.body ? { 'Content-Type': 'application/json' } : {}),
      ...options.headers,
    },
  });
  const data = await response.json();
  if (!response.ok)
    throw new ApiError(
      data.code ?? 'REQUEST_FAILED',
      data.message ?? '请求未能完成，请检查当前状态后重试。',
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
