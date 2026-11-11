import { expect, test } from '@playwright/test';

test('context indicator and manual compact keep control separate from chat', async ({ page }) => {
  let authenticated = false;
  const chat: string[] = [];
  const controls: string[] = [];
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname.replace('/api/v1', '');
    const reply = (json: unknown, status = 200) => route.fulfill({ status, json });
    if (path === '/me') return authenticated ? reply({ user_id: 'U001', display_name: '一',
      companies: [{ company_id: 'C001', name: '甲企业', aliases: [], can_update: true }],
      model_ready: true, model_mode: 'test', dataset_profile: 'competition', invoice_import_ready: false }) : reply({}, 401);
    if (path === '/demo/login') { authenticated = true; return reply({}); }
    if (path === '/sessions' && route.request().method() === 'GET') return reply([{ session_id: 's1', selected_company_id: 'C001', updated_at: '2026-11-01T00:00:00Z' }]);
    if (path.endsWith('/messages')) return reply({ turns: [], has_more: false });
    if (path === '/tasks') return reply([]);
    if (path.endsWith('/context-preview')) return reply({ input_estimate: 35000, input_limit: 92952,
      output_reserve: 2048, margin: 5000, effective_window: 100000, pressure_ratio: 0.42048,
      window_status: 'configured', count_kind: 'estimated:utf8_div2_plus_protocol' });
    if (path === '/sessions/s1/compactions' && route.request().method() === 'POST') {
      controls.push(route.request().postDataJSON().client_request_id);
      return reply({ operation_id: 'c1', status: 'queued' }, 202);
    }
    if (path === '/sessions/s1/compactions/c1') return reply({ status: 'completed', result: { outcome: 'no_content' } });
    if (path === '/sessions/s1/turns') { chat.push(route.request().postDataJSON().text); return reply({}, 503); }
    return reply({ message: 'not found' }, 404);
  });
  await page.goto('/showcase.html');
  await page.getByRole('button', { name: /测试用户一/ }).click();
  await expect(page.getByLabel('上下文估算 35%，点击查看估算详情')).toBeVisible();
  await page.getByLabel('上下文估算 35%，点击查看估算详情').click();
  await expect(page.getByText('输入约 35,000 tokens')).toBeVisible();
  await expect(page.getByText(/不含未发送草稿、预留和余量/)).toBeVisible();
  const input = page.getByRole('textbox', { name: '输入办理需求' });
  await input.fill('/compact');
  await page.getByRole('button', { name: '发送消息' }).click();
  await expect(page.getByRole('status').filter({ hasText: '暂无可压缩' })).toBeVisible();
  expect(controls).toHaveLength(1);
  expect(chat).toHaveLength(0);
  await input.fill('请解释 /compact');
  await page.getByRole('button', { name: '发送消息' }).click();
  await expect.poll(() => chat).toEqual(['请解释 /compact']);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(input).toBeInViewport();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});
