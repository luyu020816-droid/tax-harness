import { expect, test } from '@playwright/test';

test('request checklist shows verified, blocked and waiting states after reload on mobile', async ({
  page,
}) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  const session = {
    session_id: 'checklist-session',
    selected_company_id: 'C001',
    title: '事项核对程序测试',
    updated_at: '2026-11-07T00:00:00Z',
  };
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname.replace('/api/v1', '');
    const reply = (json: unknown) => route.fulfill({ json });
    if (path === '/me')
      return reply({
        user_id: 'U001',
        display_name: '程序测试',
        model_ready: true,
        model_mode: 'test',
        dataset_profile: 'competition',
        invoice_import_ready: false,
        companies: [
          {
            company_id: 'C001',
            name: '甲企业',
            aliases: [],
            can_update: true,
            can_import_invoices: false,
            can_upload_knowledge: false,
          },
        ],
      });
    if (path === '/sessions') return reply([session]);
    if (path === '/tasks') return reply([]);
    if (path.endsWith('/messages'))
      return reply({
        has_more: false,
        turns: [
          {
            turn_id: 'checklist-turn',
            source_seq: 1,
            text: '程序夹具：展示三种事项状态',
            status: 'completed',
            final_message: '程序界面测试数据，未调用真实模型。',
            next_event_seq: 0,
            active_run_generation: 1,
            error: null,
            final_cards: [
              {
                kind: 'request_progress',
                company_id: '',
                items: [
                  {
                    tool: 'query_month',
                    label: '查询月度资料',
                    company_id: 'C001',
                    period: '2026-09',
                    source_quote: '查询',
                    status: 'verified',
                  },
                  {
                    tool: 'check_purposes',
                    label: '检查发票用途',
                    company_id: 'C001',
                    period: '2026-08',
                    source_quote: '检查',
                    status: 'blocked',
                    reason: '初赛用途检查范围为 2026 年 9 月。',
                  },
                  {
                    tool: 'get_rules',
                    label: '解释适用规则',
                    company_id: 'C001',
                    period: null,
                    source_quote: '解释',
                    status: 'waiting_user',
                  },
                ],
              },
            ],
          },
        ],
      });
    if (path.endsWith('/context-usage'))
      return reply({ status: 'no_calls', input_tokens: 0, effective_window: 100000 });
    throw new Error(`Unexpected endpoint: ${path}`);
  });
  await page.goto('/showcase.html');
  const checklist = page.getByRole('region', { name: '本轮事项核对' });
  await expect(checklist).toBeVisible();
  await expect(page.getByText('已反馈，含未完成事项', { exact: true })).toBeVisible();
  await expect(checklist.getByText('已核对', { exact: true })).toBeVisible();
  await expect(checklist.getByText('无法执行', { exact: true })).toBeVisible();
  await expect(checklist.getByText('待补充', { exact: true })).toBeVisible();
  await expect(checklist.getByText('初赛用途检查范围为 2026 年 9 月。')).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.reload();
  await expect(checklist).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: '../.local/evidence/checklist-1107-mobile.png', fullPage: true });
  expect(errors).toEqual([]);
});
