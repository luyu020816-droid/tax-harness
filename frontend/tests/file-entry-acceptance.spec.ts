import { expect, test } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

test.skip(!process.env.FILE_ACCEPTANCE_DIR, 'Requires synthetic fixtures and an isolated real backend');
test.setTimeout(240_000);

for (const name of ['expenses.xlsx', 'approval.docx', 'approval.pdf']) {
  test(`original file entry parses and returns exact ${name} bytes`, async ({ page }) => {
    await page.goto('/showcase.html');
    await page.getByRole('button', { name: /测试用户一/ }).click();
    await expect(page.getByRole('button', { name: '退出登录', exact: true })).toBeVisible();
    await page.getByRole('button', { name: '知识资料', exact: true }).click();
    await page.getByRole('button', { name: '上传资料', exact: true }).click();
    const dialog = page.getByRole('dialog');
    const title = `验收原件 ${name} ${Date.now()}`;
    const path = join(process.env.FILE_ACCEPTANCE_DIR!, name);
    await dialog.getByLabel('文件', { exact: true }).setInputFiles(path);
    await dialog.getByLabel('资料标题').fill(title);
    await dialog.getByRole('button', { name: '上传并处理' }).click();
    await expect(dialog).toHaveCount(0);
    const row = page.locator('.knowledge-document').filter({ hasText: title });
    await expect(row.getByText(/当前第1版/)).toBeVisible({ timeout: 180_000 });
    await row.getByRole('button', { name: title, exact: true }).click();
    const original = page.getByRole('link', { name: /下载.*原文件/ }).first();
    await expect(original).toBeVisible();
    const href = await original.getAttribute('href');
    const response = await page.request.get(href!);
    expect(response.ok()).toBeTruthy();
    expect(Buffer.compare(await response.body(), readFileSync(path))).toBe(0);
  });
}
