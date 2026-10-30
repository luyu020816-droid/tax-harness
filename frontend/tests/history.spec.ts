import { expect, test } from '@playwright/test';

const evidenceDirectory = process.env.UI_EVIDENCE_DIRECTORY ?? '../.local/evidence/m5-history-ui';

// Requires backend/tests/history_ui_server.py: real chat model and real local BGE.
test('history: old source quote, mobile layout and invalidation', async ({ page }) => {
  test.setTimeout(300000);
  const failures: string[] = [];
  page.on('pageerror', (error) => failures.push(error.message));
  await page.goto('/');
  await page.getByRole('button', { name: /测试用户一/ }).click();
  await page.getByRole('button', { name: '新对话', exact: true }).click();
  const input = page.getByRole('textbox', { name: '输入办理需求' });
  await input.fill('请查甲企业2026年9月3日的历史对话，之前说备用投影仪放在哪里？请引用那条原话。');
  await input.press('Enter');
  const last = page.locator('.turn').last();
  await expect(
    last.locator('.history-excerpt').filter({ hasText: '西侧器材柜的第二层' })
  ).toBeVisible({ timeout: 180000 });
  await expect(last.getByText(/历史用户原文/).first()).toBeVisible();
  await page.screenshot({ path: `${evidenceDirectory}/desktop.png`, fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  if (await page.locator('.nav-backdrop').isVisible())
    await page.locator('.sidebar').getByRole('button', { name: '关闭导航' }).click();
  await expect
    .poll(() =>
      page.locator('.sidebar').evaluate((element) => element.getBoundingClientRect().right)
    )
    .toBeLessThanOrEqual(0);
  await last.locator('.history-citation').last().scrollIntoViewIfNeeded();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: `${evidenceDirectory}/mobile.png`, fullPage: true });
  const response = await page.request.post('/__test/history/suppress');
  expect(response.ok()).toBe(true);
  await expect(last.getByText(/历史引用当前不可用/)).toBeVisible({ timeout: 15000 });
  await expect(last.locator('.history-excerpt')).toHaveCount(0);
  expect(failures).toEqual([]);
});
