import { expect, test } from '@playwright/test';

test('context indicator and manual compact keep control separate from chat', async ({ page }) => {
  let authenticated = false;
  const chat: string[] = [];
  const controls: string[] = [];
  let usageKnown = true;
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname.replace('/api/v1', '');
    const reply = (json: unknown, status = 200) => route.fulfill({ status, json });
    if (path === '/me')
      return authenticated
        ? reply({
            user_id: 'U001',
            display_name: '一',
            companies: [{ company_id: 'C001', name: '甲企业', aliases: [], can_update: true }],
            model_ready: true,
            model_mode: 'test',
            dataset_profile: 'competition',
            invoice_import_ready: false,
          })
        : reply({}, 401);
    if (path === '/demo/login') {
      authenticated = true;
      return reply({});
    }
    if (path === '/sessions' && route.request().method() === 'GET')
      return reply([
        ...Array.from({ length: 8 }, (_, i) => ({
          session_id: i === 0 ? 's1' : `s${i + 1}`,
          title: `测试会话 ${i + 1}`,
          selected_company_id: 'C001',
          updated_at: '2026-11-01T00:00:00Z',
        })),
      ]);
    if (path.endsWith('/messages')) return reply({ turns: [], has_more: false });
    if (path === '/tasks') return reply([]);
    if (path.endsWith('/context-usage'))
      return reply(
        usageKnown
          ? { status: 'measured', input_tokens: 35000, effective_window: 100000 }
          : { status: 'unknown', input_tokens: null, effective_window: 100000 }
      );
    if (path === '/sessions/s1/compactions' && route.request().method() === 'POST') {
      controls.push(route.request().postDataJSON().client_request_id);
      usageKnown = false;
      return reply({ operation_id: 'c1', status: 'queued' }, 202);
    }
    if (path === '/sessions/s1/compactions/c1')
      return reply({ status: 'completed', result: { outcome: 'no_content' } });
    if (path === '/sessions/s1/turns') {
      chat.push(route.request().postDataJSON().text);
      return reply({}, 503);
    }
    return reply({ message: 'not found' }, 404);
  });
  await page.goto('/showcase.html');
  await page.getByRole('button', { name: /测试用户一/ }).click();
  const menu = page.locator('.session-actions').first();
  await menu.locator('summary').click();
  await expect(menu).toHaveAttribute('open', '');
  await page.locator('.topbar').click();
  await expect(menu).not.toHaveAttribute('open', '');
  await menu.locator('summary').click();
  await page.keyboard.press('Escape');
  await expect(menu).not.toHaveAttribute('open', '');
  for (const height of [650, 900]) {
    await page.setViewportSize({ width: 1440, height });
    const bottomTrigger = page.locator('.recent-section .session-actions summary').last();
    await bottomTrigger.scrollIntoViewIfNeeded();
    await bottomTrigger.click();
    const actions = page.locator('.session-actions-menu');
    await expect(actions.getByRole('button', { name: '删除', exact: true })).toBeInViewport();
    expect(
      await actions.evaluate((e) => {
        const r = e.getBoundingClientRect();
        return (
          e.parentElement === document.body &&
          r.top >= 0 &&
          r.bottom <= innerHeight &&
          e.contains(document.elementFromPoint(r.left + 12, r.bottom - 12))
        );
      })
    ).toBe(true);
    await page.locator('.topbar').click();
    await expect(actions).toHaveCount(0);
  }
  await expect(page.getByLabel('上下文用量 35%，点击查看详情')).toBeVisible();
  await page.getByLabel('上下文用量 35%，点击查看详情').click();
  await expect(page.getByText('输入 35,000 tokens')).toBeVisible();
  await expect(page.getByText(/不累计历史调用/)).toBeVisible();
  const popover = page.locator('.context-indicator-popover');
  expect(await popover.evaluate((e) => e.parentElement === document.body)).toBe(true);
  expect(
    await popover.evaluate((e) => {
      const rect = e.getBoundingClientRect();
      return e.contains(document.elementFromPoint(rect.left + 10, rect.top + 10));
    })
  ).toBe(true);
  await page.keyboard.press('Escape');
  await expect(popover).toHaveCount(0);
  await page.getByLabel('上下文用量 35%，点击查看详情').click();
  await page.locator('.topbar').click();
  await expect(popover).toHaveCount(0);
  const input = page.getByRole('textbox', { name: '输入办理需求' });
  await input.fill('/compact');
  await page.getByRole('button', { name: '发送消息' }).click();
  await expect(page.getByRole('status').filter({ hasText: '暂无可压缩' })).toBeVisible();
  await expect(page.getByLabel('上下文用量 --，点击查看详情')).toBeVisible();
  await page.getByLabel('上下文用量 --，点击查看详情').click();
  await expect(page.getByText('实际输入未知')).toBeVisible();
  expect(controls).toHaveLength(1);
  expect(chat).toHaveLength(0);
  await input.fill('请解释 /compact');
  await page.getByRole('button', { name: '发送消息' }).click();
  await expect.poll(() => chat).toEqual(['请解释 /compact']);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(input).toBeInViewport();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});
