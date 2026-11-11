import { expect, test } from '@playwright/test';

test('a verified query result stays visible when final reply exceeds budget', async ({ page }) => {
  const turn = {
    turn_id: 't1', text: '查询甲企业2026年9月发票', status: 'failed', source_seq: 1,
    final_message: '发票查询已成功，以下是核验时仍有效的查询结果。本轮后续答复因上下文预算不足未完成。',
    final_cards: [{ kind: 'invoices', company_id: 'C001', period: '2026-09', rows: [], total: 0, offset: 0, has_more: false }],
    error: { code: 'CONTEXT_CAPACITY_EXCEEDED', message: '查询成功但生成答复失败' }, runs: [],
  };
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname.replace('/api/v1', '');
    const reply = (json: unknown) => route.fulfill({ status: 200, json });
    if (path === '/me') return reply({ user_id: 'U001', display_name: '一',
      companies: [{ company_id: 'C001', name: '甲', can_update: true }], model_ready: true,
      model_mode: 'test', dataset_profile: 'competition', invoice_import_ready: false });
    if (path === '/sessions') return reply([{ session_id: 's1', selected_company_id: 'C001', title: '查询', updated_at: '2026-11-01' }]);
    if (path.endsWith('/messages')) return reply({ turns: [turn], has_more: false });
    if (path === '/tasks') return reply([]);
    if (path.endsWith('/context-usage')) return reply({ status: 'no_calls', input_tokens: 0, effective_window: 100000 });
    return reply({});
  });
  await page.goto('/showcase.html');
  await expect(page.locator('.prose')).toContainText('发票查询已成功');
  await expect(page.getByText('找到 0 张发票')).toBeVisible();
  await expect(page.getByText('查询结果已交付，后续未完成')).toBeVisible();
});
