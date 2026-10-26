import { expect, test } from '@playwright/test';

test.use({ baseURL: 'http://127.0.0.1:8103' });

test('IMP-20: offline upload, preview, append receipt, history and scope', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto('/');
  await page.getByRole('button', { name: /测试用户一/ }).click();
  await expect(page.getByText('扩展数据集', { exact: false }).first()).toBeVisible();
  await expect(page.getByRole('button', { name: '发送消息' })).toBeDisabled();
  await page.getByRole('button', { name: '导入发票 Excel' }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('选择发票 Excel').setInputFiles('../examples/invoice-append.xlsx');
  await expect(dialog.getByText('等待确认', { exact: true })).toBeVisible();
  await expect(dialog.getByRole('button', { name: '向甲企业追加 1 张发票' })).toBeEnabled();
  await page.screenshot({ path: '../.local/evidence/import/01-preview.png', fullPage: true });
  await page.setViewportSize({ width: 360, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: '../.local/evidence/import/02-narrow.png', fullPage: true });
  await dialog.getByRole('button', { name: '向甲企业追加 1 张发票' }).click();
  await expect(dialog.getByRole('heading', { name: '已追加', exact: true })).toBeVisible();
  await expect(dialog.getByText('本批次新增 1 张，跳过 0 行。')).toBeVisible();
  const invoices = await page.request.get('/api/v1/companies/C001/invoices?period=2026-09');
  expect((await invoices.json()).total).toBe(11);
  await page.screenshot({ path: '../.local/evidence/import/03-receipt.png', fullPage: true });
  await page.keyboard.press('Escape');
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.getByRole('button', { name: '运行记录', exact: true }).click();
  await page.getByRole('button', { name: '数据导入', exact: true }).click();
  await page.getByRole('button', { name: /invoice-append.xlsx/ }).click();
  await expect(dialog.getByRole('heading', { name: '已追加', exact: true })).toBeVisible();
  await page.keyboard.press('Escape');
  await page.getByLabel('当前模拟用户').selectOption('U002');
  await page.getByRole('button', { name: '对话', exact: true }).click();
  await expect(page.getByRole('button', { name: '导入发票 Excel' })).toHaveCount(0);
  expect(errors).toEqual([]);
});

test('IMP-07: conflicts cannot commit until explicit skip preview', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: /测试用户一/ }).click();
  await page.getByRole('button', { name: '导入发票 Excel' }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('选择发票 Excel').setInputFiles('../.local/test-inputs/conflicts.xlsx');
  await expect(dialog.getByRole('heading', { name: '需要处理问题' })).toBeVisible();
  await expect(dialog.getByRole('button', { name: '向甲企业追加 1 张发票' })).toBeDisabled();
  await dialog.getByRole('checkbox').check();
  await dialog.getByRole('button', { name: '重新预览', exact: true }).click();
  await expect(dialog.getByRole('button', { name: '向甲企业追加 1 张发票' })).toBeEnabled();
  await dialog.getByRole('button', { name: '向甲企业追加 1 张发票' }).click();
  await expect(dialog.getByText('本批次新增 1 张，跳过 2 行。')).toBeVisible();
});
