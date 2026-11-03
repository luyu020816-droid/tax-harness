import { expect, test } from '@playwright/test';

test('live history and manual text entry are reachable without submitting data', async ({
  page,
}, info) => {
  test.skip(
    process.env.INSIGHTS_LIVE !== '1',
    'Requires explicitly selected local backend; read-only UI verification'
  );
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  expect((await page.request.post('/api/v1/demo/login', { data: { user_id: 'U001' } })).ok()).toBe(
    true
  );
  await page.goto('/showcase.html');
  await page.getByRole('button', { name: '全部会话与归档', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '历史会话', exact: true });
  await expect(dialog.locator('.session-history-row').first()).toBeVisible();
  await page.screenshot({ path: info.outputPath('live-history-desktop.png'), fullPage: true });
  await dialog.getByRole('button', { name: '已归档', exact: true }).click();
  await expect(dialog.getByRole('button', { name: '全部会话', exact: true })).toBeVisible();
  await dialog.getByRole('button', { name: '关闭', exact: true }).click();
  await page.getByRole('button', { name: '知识资料', exact: true }).click();
  await page.getByRole('button', { name: '添加文字', exact: true }).click();
  const text = page.getByRole('dialog', { name: '添加文字资料', exact: true });
  await expect(text.getByLabel('资料正文', { exact: true })).toBeVisible();
  await expect(text.getByRole('button', { name: '提交到知识库', exact: true })).toBeDisabled();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await text.getByLabel('资料正文', { exact: true }).click();
  await page.screenshot({
    path: info.outputPath('live-text-mobile.png'),
    fullPage: true,
    animations: 'disabled',
  });
  await text.getByRole('button', { name: '关闭', exact: true }).click();
  await page.getByRole('button', { name: '退出登录', exact: true }).click();
  await expect(page.getByRole('button', { name: /测试用户一/ })).toBeVisible();
  expect((await page.request.get('/api/v1/me')).status()).toBe(401);
  expect(errors).toEqual([]);
});
