import { expect, test } from '@playwright/test';

test('confirmed plan saves once, survives reload, and hides after identity change', async ({
  page,
}) => {
  const failures: string[] = [];
  page.on('pageerror', (error) => failures.push(error.message));
  await page.goto('/');
  await page.getByRole('button', { name: /测试用户一/ }).click();
  const input = page.getByRole('textbox', { name: '输入办理需求' });
  await input.fill('查询甲企业2026年9月月度资料');
  await input.press('Enter');
  await expect(page.getByText('9,622.06', { exact: false })).toBeVisible();
  await input.fill('请安排把甲企业F002用途填写为办公耗材采购。');
  await input.press('Enter');
  const card = page.locator('.write-plan-card');
  await expect(card.getByRole('heading', { name: '请核对后确认保存' })).toBeVisible();
  await expect(card.getByText('办公耗材采购', { exact: true })).toBeVisible();
  await page.reload();
  await expect(card.getByRole('button', { name: '确认并保存' })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await card.getByRole('button', { name: '确认并保存' }).scrollIntoViewIfNeeded();
  await expect(page.locator('.sidebar')).not.toBeInViewport();
  await expect(card.getByRole('button', { name: '确认并保存' })).toBeInViewport();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({
    path: '../.local/evidence/patch002-m1-ui/confirmation-mobile.png',
    fullPage: true,
    animations: 'disabled',
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.screenshot({
    path: '../.local/evidence/patch002-m1-ui/confirmation-desktop.png',
    fullPage: true,
  });
  await card.getByRole('button', { name: '确认并保存' }).click();
  await expect(page.getByRole('heading', { name: '已保存并核验', exact: true })).toBeVisible();
  await expect(
    page.locator('.turn').nth(1).locator('.answer-footer').getByText('已解决')
  ).toBeVisible();
  await expect(card.getByRole('heading', { name: '该修改已执行，请核对操作回执' })).toBeVisible();
  await page.screenshot({ path: '../.local/evidence/patch002-m1-ui/verified.png', fullPage: true });
  const turns = await page.locator('.turn').count();
  await card.getByRole('button', { name: '查看办理结果' }).click();
  await expect(page.locator('.turn')).toHaveCount(turns);
  await page.getByLabel('当前模拟用户').selectOption('U002');
  await expect(page.locator('.write-plan-card')).toHaveCount(0);
  await expect(page.locator('.receipt-card')).toHaveCount(0);
  expect(failures).toEqual([]);
});
