import { expect, test } from '@playwright/test';

// Deliberately uses the real preview and local retrieval dependencies. No model stub.
test('knowledge: private upload, semantic search, quote, new version, disable and isolation', async ({
  page,
}) => {
  test.setTimeout(300000);
  const evidenceRoot = process.env.UI_EVIDENCE_DIRECTORY ?? '../.local/evidence/patch002-knowledge-ui';
  const failures: string[] = [];
  page.on('pageerror', (error) => failures.push(error.message));
  const title = `差旅审批联调 ${Date.now()}`;
  await page.goto('/');
  await page.getByRole('button', { name: /测试用户一/ }).click();
  await page.getByRole('button', { name: '新对话', exact: true }).click();
  await page.getByRole('button', { name: '知识资料', exact: true }).click();
  await page.getByRole('button', { name: '上传资料', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('文件', { exact: true }).setInputFiles({
    name: '出差制度.txt',
    mimeType: 'text/plain',
    buffer: Buffer.from(
      '出差住宿费必须由部门负责人事先批准。未经批准不得报销酒店费用。紧急出行可以申请加急审批，但不得省略批准手续。'
    ),
  });
  await dialog.getByLabel('资料标题').fill(title);
  await dialog.getByRole('button', { name: '上传并建立索引' }).click();
  await expect(dialog).toHaveCount(0);
  const row = page.locator('.knowledge-document').filter({ hasText: title });
  await expect(row.getByText(/当前第1版/)).toBeVisible({ timeout: 120000 });
  await page
    .getByRole('textbox', { name: '检索知识资料' })
    .fill('临时着急出门住店，可以不走批准手续吗？');
  await page.getByRole('button', { name: '查找依据' }).click();
  const citation = page
    .locator('.knowledge-results .knowledge-citation')
    .filter({ hasText: title });
  await expect(citation.getByText(/但不得省略批准手续/)).toBeVisible({ timeout: 90000 });
  await page.screenshot({
    path: `${evidenceRoot}/search-desktop.png`,
    fullPage: true,
  });
  await citation.getByRole('button').click();
  await expect(page.getByRole('link', { name: '下载引用版本原文件' })).toBeVisible();
  await expect(page.locator('.drawer').getByText(/第1.*行/)).toBeVisible();
  await page.getByRole('button', { name: '关闭', exact: true }).click();
  await row.getByRole('button', { name: '用于对话' }).click();
  const input = page.getByRole('textbox', { name: '输入办理需求' });
  await input.fill('请查阅这份差旅制度，紧急出差住宿可以跳过事先批准吗？');
  await input.press('Enter');
  await expect(
    page
      .locator('.turn')
      .last()
      .locator('.knowledge-excerpt')
      .getByText(/但不得省略批准手续/)
  ).toBeVisible({ timeout: 150000 });
  await page.setViewportSize({ width: 390, height: 844 });
  if (await page.locator('.nav-backdrop').isVisible())
    await page.locator('.sidebar').getByRole('button', { name: '关闭导航' }).click();
  await page.locator('.turn .knowledge-citation').last().scrollIntoViewIfNeeded();
  await expect(page.locator('.sidebar')).not.toBeInViewport();
  await expect(page.locator('.turn .knowledge-citation').last()).toBeInViewport();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({
    path: `${evidenceRoot}/quote-mobile.png`,
    fullPage: true,
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.getByRole('button', { name: '知识资料', exact: true }).click();
  await row.getByRole('button', { name: '上传新版' }).click();
  await dialog.getByLabel('文件', { exact: true }).setInputFiles({
    name: '出差制度v2.txt',
    mimeType: 'text/plain',
    buffer: Buffer.from('差旅住宿必须由财务负责人事先批准。紧急情况仍须批准。'),
  });
  await dialog.getByRole('button', { name: '上传并建立索引' }).click();
  await expect(row.getByText(/当前第2版/)).toBeVisible({ timeout: 120000 });
  await row.getByRole('button', { name: '停用', exact: true }).click();
  await expect(row.getByText(/已停用/)).toBeVisible();
  await page.getByRole('button', { name: '对话', exact: true }).click();
  await expect(
    page
      .locator('.turn')
      .last()
      .getByText(/引用当前不可用/)
  ).toBeVisible({ timeout: 10000 });
  await expect(page.locator('.turn').last().locator('.knowledge-excerpt')).toHaveCount(0);
  await page.getByLabel('当前模拟用户').selectOption('U002');
  await page.getByRole('button', { name: '知识资料', exact: true }).click();
  await expect(page.locator('.knowledge-document').filter({ hasText: title })).toHaveCount(0);
  expect(failures).toEqual([]);
});
