import { expect, test, type Page } from '@playwright/test';

const evidence = '../.local/evidence/ui';
async function shot(page: Page, name: string) {
  await page.screenshot({ path: `${evidence}/${name}.png`, fullPage: true });
  // Optional readable walkthrough recording; ordinary regression runs do not pause.
  if (process.env.UI_RECORD === '1') await page.waitForTimeout(6000);
}

test('real browser: results, saves, memory, traces, responsive and identity', async ({ page }) => {
  const failures: string[] = [];
  page.on('pageerror', error => failures.push(error.message));
  await page.goto('/');
  await page.getByRole('button', { name: /测试用户一/ }).click();
  await expect(page.getByText('程序测试模式', { exact: false })).toBeVisible();
  await expect(page.getByRole('button', { name: '发送消息' })).toBeDisabled();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await shot(page, '01-empty-desktop');
  const input = page.getByRole('textbox', { name: '输入办理需求' });
  await input.fill('查询甲企业2026年9月月度资料');
  await page.getByRole('button', { name: '发送消息' }).click();
  await expect(page.getByText('9,622.06', { exact: false })).toBeVisible();
  await expect(page.getByText('13,000.50', { exact: false })).toBeVisible();
  await shot(page, '02-monthly-summary');
  await input.fill('检查甲企业2026年9月发票用途');
  await input.press('Enter');
  await expect(page.getByRole('heading', { name: /待补.*5.*张发票用途/ })).toBeVisible();
  await shot(page, '03-purpose-check');
  await input.fill('甲企业F002用途补为办公耗材采购');
  await input.press('Enter');
  await expect(page.getByText('F002用途已保存并回读核验', { exact: false })).toBeVisible();
  await expect(page.getByRole('heading', { name: /待补.*4.*张发票用途/ })).toBeVisible();
  await shot(page, '04-save-verified');
  await page.getByRole('button', { name: '记忆管理', exact: true }).click();
  await page.getByRole('button', { name: /^偏好/ }).click();
  await page.getByRole('button', { name: /记住.*偏好/ }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toBeVisible();
  await dialog.getByRole('textbox').fill('以后请先列出需要补用途的发票');
  await dialog.getByRole('button', { name: '保存偏好' }).click();
  await expect(page.getByText('以后请先列出需要补用途的发票')).toBeVisible();
  await shot(page, '05-memory');
  await page.getByRole('button', { name: '更正偏好' }).click();
  await dialog.getByRole('textbox').fill('以后回复简洁，并保留规则依据');
  await dialog.getByRole('button', { name: '保存偏好' }).click();
  await expect(page.getByText('以后回复简洁，并保留规则依据')).toBeVisible();
  await page.getByRole('button', { name: '删除偏好' }).click();
  await dialog.getByRole('button', { name: '确认删除' }).click();
  await expect(page.getByText('还没有长期偏好')).toBeVisible();
  await page.getByRole('button', { name: '运行记录', exact: true }).click();
  await page.locator('.run-row').first().click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await shot(page, '06-run-details');
  await dialog.getByRole('button', { name: '模型输入', exact: true }).click();
  await dialog.locator('details').first().locator('summary').click();
  await expect(dialog.getByRole('heading', { name: '实际请求' }).first()).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await page.getByRole('button', { name: '对话', exact: true }).click();
  for (const width of [1280, 768, 390, 360]) {
    await page.setViewportSize({ width, height: width < 500 ? 844 : 720 });
    await expect(input).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await shot(page, `07-responsive-${width}`);
  }
  await input.fill('中文输入法候选');
  const before = await page.locator('.turn').count();
  await input.dispatchEvent('compositionstart');
  await input.press('Enter');
  expect(await page.locator('.turn').count()).toBe(before);
  await expect(input).toHaveValue(/^中文输入法候选/);
  await input.dispatchEvent('compositionend');
  await page.getByLabel('当前企业').selectOption('C002');
  await expect(page.locator('.turn')).toHaveCount(0);
  await expect(page.locator('.task-strip')).toHaveCount(0);
  await page.getByLabel('当前模拟用户').selectOption('U002');
  await expect(page.getByText('只读', { exact: true })).toBeVisible();
  await expect(page.getByText('9,622.06', { exact: false })).toHaveCount(0);
  await expect(page.getByLabel('当前企业').locator('option')).toHaveCount(2);
  expect(failures).toEqual([]);
});
