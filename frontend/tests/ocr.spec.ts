import { expect, test } from '@playwright/test';

// Real scanned PDF, real parser job and local BGE; no mocked network or vectors.
test('scanned PDF: page citations, original file, failure preserves active version', async ({
  page,
}) => {
  test.setTimeout(600000);
  const fixture = process.env.OCR_FIXTURE_PATH;
  const low = process.env.OCR_LOW_FIXTURE_PATH;
  if (!fixture || !low) throw new Error('Set OCR_FIXTURE_PATH and OCR_LOW_FIXTURE_PATH');
  const errors: string[] = [];
  page.on('pageerror', (e) => errors.push(e.message));
  const title = `扫描制度人工验收样本 ${Date.now()}`;
  await page.goto('/');
  await page.getByRole('button', { name: /测试用户一/ }).click();
  await page.getByRole('button', { name: '知识资料', exact: true }).click();
  await page.getByRole('button', { name: '上传资料', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('文件', { exact: true }).setInputFiles(fixture);
  await dialog.getByLabel('资料标题').fill(title);
  await dialog.getByRole('button', { name: '上传并建立索引' }).click();
  const row = page.locator('.knowledge-document').filter({ hasText: title });
  await expect(row.getByText(/当前第1版/)).toBeVisible({ timeout: 240000 });
  await page
    .getByRole('textbox', { name: '检索知识资料' })
    .fill('内部资料可以直接外发吗，需要什么批准，哪些例外，能公开手机号吗？');
  await page.getByRole('button', { name: '查找依据' }).click();
  const citation = page
    .locator('.knowledge-results .knowledge-citation')
    .filter({ hasText: title })
    .first();
  await expect(citation.getByText(/不允许公开客户手机号/)).toBeVisible({ timeout: 120000 });
  await expect(citation.getByText(/OCR识别/)).toBeVisible();
  await citation.getByRole('button').click();
  await expect(page.getByRole('link', { name: '下载引用版本原文件' })).toBeVisible();
  await expect(page.locator('.drawer').getByText(/扫描件识别文字/)).toBeVisible();
  await page.screenshot({
    path: `${process.env.UI_EVIDENCE_DIRECTORY}/ocr-desktop.png`,
    fullPage: true,
  });
  await page.getByRole('button', { name: '关闭', exact: true }).click();
  await row.getByRole('button', { name: '上传新版' }).click();
  await dialog.getByLabel('文件', { exact: true }).setInputFiles(low);
  await dialog.getByRole('button', { name: '上传并建立索引' }).click();
  await expect(row.getByText(/低于100dpi/)).toBeVisible({ timeout: 120000 });
  await expect(row.getByText(/当前第1版/)).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: `${process.env.UI_EVIDENCE_DIRECTORY}/ocr-mobile.png`,
    fullPage: true,
  });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(errors).toEqual([]);
});
