import { expect, test } from '@playwright/test';

test('agent is configured and used against the real program-test backend', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/showcase.html');
  await page.getByRole('button', { name: /测试用户一/ }).click();
  await page.getByRole('button', { name: 'Agent 管理' }).click();
  const dialog = page.getByRole('dialog', { name: 'Agent 与能力管理' });
  await dialog.getByLabel('名称', { exact: true }).fill('实测账务助手');
  await dialog.getByLabel('职责提示词').fill('查询前核对企业和月份');
  await dialog
    .getByText(/工具和动作授权/)
    .first()
    .click();
  for (const name of ['query_month', 'resolve_scope']) {
    await dialog
      .getByRole('group', { name, exact: true })
      .getByRole('checkbox', { name: 'invoke' })
      .check();
  }
  await dialog.getByRole('button', { name: '保存草稿' }).click();
  await expect(dialog.getByRole('status')).toContainText('草稿已保存');
  await dialog.getByRole('button', { name: '发布已保存草稿' }).click();
  await expect(dialog.getByRole('status')).toContainText('已发布');
  await dialog.getByRole('button', { name: '启用', exact: true }).first().click();
  await expect(dialog.getByRole('status')).toContainText('状态已更新');
  await dialog.getByRole('button', { name: '关闭' }).click();
  const agents = page.getByLabel('当前 Agent');
  await expect(agents.locator('option', { hasText: '实测账务助手' })).toHaveCount(1);
  await agents.selectOption({ label: '实测账务助手' });
  await page.getByRole('textbox', { name: '输入办理需求' }).fill('查询甲企业2026-09月度资料');
  await page.getByRole('button', { name: '发送消息' }).click();
  await expect(page.locator('body')).toContainText('9,622.06', { timeout: 30000 });
});
