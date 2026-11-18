import { expect, test } from '@playwright/test';

test.skip(
  !process.env.WORKBOOK_E2E_FILE,
  'Requires an isolated server with a configured real model and a sample workbook'
);

for (const scenario of [
  {
    label: 'standard headers',
    file: process.env.WORKBOOK_E2E_FILE,
    october: '合计 617.00 元 · 4 行',
    group: '交通费 · 180.00 元 · 3 行',
    source: '第 4 行（C4）',
    november: '合计 88.50 元 · 1 行',
    followupSource: '第 6 行（C6）',
  },
  {
    label: 'reordered alias headers',
    file: process.env.WORKBOOK_E2E_OTHER_FILE,
    october: '合计 32.60 元 · 3 行',
    group: '交通费 · 12.40 元 · 2 行',
    source: '第 4 行（B4）',
    november: '合计 14.50 元 · 1 行',
    followupSource: '第 5 行（B5）',
  },
]) {
  test(`calculates two scopes and opens original evidence with ${scenario.label}`, async ({
    page,
  }, testInfo) => {
    test.skip(!scenario.file, 'Requires the matching synthetic workbook');
    await page.goto('/showcase.html');
    await page.getByRole('button', { name: /测试用户一/ }).click();
    await expect(page.getByRole('textbox', { name: '输入办理需求' })).toBeVisible();
    const created = page
      .waitForResponse(
        (response) =>
          new URL(response.url()).pathname === '/api/v1/sessions' &&
          response.request().method() === 'POST' &&
          response.status() === 201
      )
      .then((response) => response.json() as Promise<{ session_id: string }>);
    const loadsNewSession = page.waitForResponse(async (response) => {
      const path = new URL(response.url()).pathname;
      if (!/\/sessions\/[^/]+\/messages$/.test(path) || !response.ok()) return false;
      return path === `/api/v1/sessions/${(await created).session_id}/messages`;
    });
    await page.getByRole('button', { name: '新对话', exact: true }).click();
    await loadsNewSession;
    await page.getByRole('button', { name: '知识资料', exact: true }).click();
    await page.getByRole('button', { name: '上传资料', exact: true }).click({ timeout: 12000 });
    const upload = page.getByRole('dialog', { name: '上传知识资料' });
    await upload.locator('input[type=file]').setInputFiles(scenario.file!);
    const title = `Excel 验收 ${Date.now()}`;
    await upload.getByLabel('资料标题').fill(title);
    await upload.getByRole('button', { name: '上传并处理' }).click();
    const doc = page.locator('article').filter({ hasText: title });
    await expect(doc.getByRole('button', { name: '用于对话' })).toBeEnabled({ timeout: 25000 });
    await doc.getByRole('button', { name: '用于对话' }).click();
    const agents = page.getByLabel('当前 Agent');
    await agents.selectOption({ label: '财税办理助手' });
    const input = page.getByRole('textbox', { name: '输入办理需求' });
    await input.fill('2026年10月实付报销合计多少？按费用类别拆开。');
    await page.getByRole('button', { name: '发送消息' }).click();
    const card = page.getByRole('region', { name: 'Excel 计算结果' });
    await expect(card).toContainText(scenario.october, { timeout: 50000 });
    await expect(card).toContainText(scenario.group);
    await card.getByText('查看原表依据').click();
    await expect(card).toContainText(scenario.source);
    await expect(card.getByRole('link', { name: '下载当前有效原表' })).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath('october.png'), fullPage: true });
    await input.fill('改成11月，只看交通费');
    await page.getByRole('button', { name: '发送消息' }).click();
    const nextCard = card.last();
    await expect(nextCard).toContainText(scenario.november, { timeout: 50000 });
    await nextCard.getByText('查看原表依据').click();
    await expect(nextCard).toContainText(scenario.followupSource);
    await page.screenshot({ path: testInfo.outputPath('november.png'), fullPage: true });
  });
}
