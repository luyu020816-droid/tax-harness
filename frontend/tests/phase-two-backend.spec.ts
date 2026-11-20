import { expect, test } from '@playwright/test';

test('memory history and knowledge strategy use the real isolated backend', async ({ page }) => {
  await page.goto('/showcase.html');
  await page.getByRole('button', { name: /测试用户一/ }).click();
  await expect(page.getByRole('button', { name: '退出登录', exact: true })).toBeVisible();
  const key = `audit_${Date.now()}`;
  const created = await page.request.post('/api/v1/preferences', {
    data: { key, value: '回复简洁', request_id: crypto.randomUUID() },
  });
  expect(created.ok()).toBeTruthy();
  const pref = await created.json();
  const removed = await page.request.delete(`/api/v1/preferences/${pref.preference_id}`, {
    data: { expected_revision: pref.revision, request_id: crypto.randomUUID() },
  });
  expect(removed.ok()).toBeTruthy();
  await page.getByRole('button', { name: '记忆管理', exact: true }).click();
  await page.getByRole('button', { name: '变更记录', exact: true }).click();
  const row = page.locator('article').filter({ hasText: key });
  await expect(row).toContainText('已删除');
  await row.getByRole('button', { name: '查看修订' }).click();
  await expect(page.getByRole('region', { name: '偏好修订' })).toContainText('回复简洁');
  await page.getByRole('button', { name: '知识资料', exact: true }).click();
  await page.getByText(/知识库检索策略/).click();
  const strategy = page
    .locator('details')
    .filter({ has: page.locator('summary', { hasText: '知识库检索策略' }) })
    .first();
  await expect(strategy.getByRole('button', { name: '发布检索策略' })).toBeVisible();
  await strategy.getByLabel('最多完整条款组').fill('2');
  await strategy.getByRole('button', { name: '发布检索策略' }).click();
  await expect(strategy.getByRole('status')).toContainText('检索策略已发布');
  const actual = await page.request.get('/api/v1/knowledge/strategies/C001');
  expect((await actual.json()).config.max_groups).toBe(2);

  // Exercise the original Agent selector, draft estimate, and manual control path.
  const headers = { Origin: new URL(page.url()).origin };
  const draft = await page.request.post('/api/v1/agent-admin/agents', {
    headers,
    data: {
      name: `压缩助手${Date.now()}`,
      model_profile_id: 'default',
      knowledge_mode: 'none',
      tool_grants: {},
    },
  });
  expect(draft.ok()).toBeTruthy();
  let profile = await draft.json();
  const published = await page.request.post(
    `/api/v1/agent-admin/agents/${profile.agent_id}/publish`,
    { headers, data: { expected_revision: profile.revision } }
  );
  expect(published.ok()).toBeTruthy();
  profile = await published.json();
  expect(
    (
      await page.request.post(`/api/v1/agent-admin/agents/${profile.agent_id}/enabled`, {
        headers,
        data: { expected_revision: profile.revision, enabled: true },
      })
    ).ok()
  ).toBeTruthy();
  // Reopen the workbench so its catalog reflects the configuration just created.
  await page.reload();
  await page.getByLabel('当前 Agent').selectOption(profile.agent_id);
  await page.getByRole('button', { name: /上下文用量.*点击查看详情/ }).click();
  await expect(page.getByRole('status', { name: '上下文详情' })).toContainText('下一次输入预估');
  await expect(page.getByRole('status', { name: '上下文详情' })).toContainText('工具 0 项');
  await page.getByRole('textbox', { name: '输入办理需求' }).fill('/compact');
  const accepted = page.waitForResponse(
    (r) => r.url().endsWith('/compactions') && r.request().method() === 'POST'
  );
  await page.getByRole('button', { name: '发送消息' }).click();
  const response = await accepted;
  expect(response.status()).toBe(202);
  expect(response.request().postDataJSON().agent_id).toBe(profile.agent_id);
  const receipt = await response.json();
  const statusUrl = `${new URL(response.url()).pathname}/${receipt.operation_id}`;
  await expect
    .poll(async () => (await (await page.request.get(statusUrl)).json()).status)
    .toBe('completed');
});
