import { expect, test } from '@playwright/test';

for (const mode of [
  'network',
  'invalid-success',
  'invalid-error',
  'missing-turn',
  'json-503',
  'denied',
]) {
  test(`${mode}: distinguish unknown status from explicit rejection`, async ({ page }) => {
    const session = {
      session_id: 'network-session',
      selected_company_id: 'C001',
      title: '网络恢复程序测试',
      updated_at: '2026-11-07T00:00:00Z',
    };
    const requests: { client_request_id: string; text: string }[] = [];
    let accepted = false;
    const turn = {
      turn_id: 'one-turn',
      source_seq: 1,
      text: '程序测试：补充用途',
      status: 'completed',
      final_message: '原请求已核对（程序夹具）',
      final_cards: [],
      next_event_seq: 0,
      active_run_generation: 1,
      error: null,
    };
    await page.route('**/api/v1/**', async (route) => {
      const path = new URL(route.request().url()).pathname.replace('/api/v1', '');
      const reply = (json: unknown) => route.fulfill({ json });
      if (path === '/me')
        return reply({
          user_id: 'U001',
          display_name: '程序测试',
          model_ready: true,
          model_mode: 'test',
          dataset_profile: 'competition',
          invoice_import_ready: false,
          companies: [
            {
              company_id: 'C001',
              name: '甲企业',
              aliases: [],
              can_update: true,
              can_import_invoices: false,
              can_upload_knowledge: false,
            },
          ],
        });
      if (path === '/sessions') return reply([session]);
      if (path === '/tasks' || path === '/agents') return reply([]);
      if (path === '/turns/one-turn') return reply(turn);
      if (path === '/turns/one-turn/events') return route.fulfill({ status: 204 });
      if (path.endsWith('/messages'))
        return reply({ has_more: false, turns: accepted ? [turn] : [] });
      if (path === '/sessions/network-session/turns' && route.request().method() === 'POST') {
        requests.push(route.request().postDataJSON());
        accepted = true; // Simulates acceptance followed by a lost response, not real business data.
        if (requests.length === 1) {
          if (mode === 'invalid-success') return route.fulfill({ status: 200, json: null });
          if (mode === 'missing-turn') return route.fulfill({ status: 200, json: {} });
          if (mode === 'invalid-error') return route.fulfill({ status: 503, json: null });
          if (mode === 'json-503')
            return route.fulfill({
              status: 503,
              json: { code: 'DB_UNAVAILABLE', message: '数据库操作未能完成' },
            });
          if (mode === 'denied')
            return route.fulfill({
              status: 403,
              json: { code: 'ACCESS_DENIED', message: '只读身份不能修改' },
            });
          return route.abort('connectionfailed');
        }
        return reply(turn);
      }
      if (path.endsWith('/context-usage'))
        return reply({ status: 'no_calls', input_tokens: 0, effective_window: 100000 });
      throw new Error(`Unexpected endpoint: ${path}`);
    });
    await page.goto('/showcase.html');
    const input = page.getByRole('textbox', { name: '输入办理需求' });
    await input.fill(turn.text);
    await page.getByRole('button', { name: '发送消息' }).click();
    if (mode === 'denied') {
      await expect(page.getByRole('alert')).toContainText('只读身份不能修改');
      await expect(page.getByRole('button', { name: '核对原请求', exact: true })).toHaveCount(0);
      await expect(input).toHaveValue(turn.text);
      expect(requests).toHaveLength(1);
      return;
    }
    await expect(page.getByRole('alert')).toContainText('处理状态待核对');
    await expect(page.getByRole('alert')).toContainText('保持内容不变再次发送');
    await expect(input).toHaveValue(turn.text);
    await input.fill(turn.text + '已改变');
    await expect(page.getByRole('button', { name: '核对原请求', exact: true })).toHaveCount(0);
    await input.fill(turn.text);
    await page.setViewportSize({ width: 390, height: 844 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(
      true
    );
    await page.getByRole('button', { name: '核对原请求', exact: true }).click();
    await expect(page.getByText(turn.final_message, { exact: true })).toBeVisible();
    expect(requests).toHaveLength(2);
    expect(requests[1].client_request_id).toBe(requests[0].client_request_id);
    await expect(page.locator('.turn')).toHaveCount(1);
  });
}

test('verify the original operation before offering an explicit continuation', async ({ page }) => {
  const session = {
    session_id: 'receipt-session',
    selected_company_id: 'C001',
    title: '恢复验收',
    updated_at: '2026-11-20T00:00:00Z',
  };
  const prepared = {
    kind: 'operation',
    operation_id: '67a3787c-4a92-449c-9c0c-bac71b7e3e04',
    company_id: 'C001',
    invoice_id: 'F002',
    commit_state: 'prepared',
    verification_state: 'pending',
    patch: { purpose: '办公耗材采购' },
    saved_values: null,
  };
  const receipt = {
    ...prepared,
    commit_state: 'committed',
    verification_state: 'verified',
    saved_values: prepared.patch,
  };
  const failed = {
    turn_id: 'save-turn',
    source_seq: 1,
    text: '把甲企业F002用途填写为办公耗材采购。',
    status: 'failed',
    final_message: null,
    final_cards: [],
    invoice_operations: [prepared],
    next_event_seq: 0,
    active_run_generation: 1,
    error: { code: 'COMMIT_STATUS_UNKNOWN', message: '保存结果暂时无法确认。' },
  };
  const completed = {
    ...failed,
    status: 'completed',
    final_message: '保存结果已核对。',
    final_cards: [receipt],
    invoice_operations: [receipt],
    error: null,
    active_run_generation: 2,
  };
  let checks = 0;
  let resumes = 0;
  const operationPath = `/turns/save-turn/operations/${prepared.operation_id}`;
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname.replace('/api/v1', '');
    const reply = (json: unknown) => route.fulfill({ json });
    if (path === '/me')
      return reply({
        user_id: 'U001',
        display_name: '程序测试',
        model_ready: true,
        model_mode: 'test',
        dataset_profile: 'competition',
        companies: [
          {
            company_id: 'C001',
            name: '甲企业',
            aliases: [],
            can_update: true,
            can_import_invoices: false,
            can_upload_knowledge: false,
          },
        ],
      });
    if (path === '/sessions') return reply([session]);
    if (path === '/tasks' || path === '/agents') return reply([]);
    if (path.endsWith('/messages'))
      return reply({ has_more: false, turns: [resumes ? completed : failed] });
    if (path.endsWith('/context-usage')) return reply({ status: 'no_calls' });
    if (path === '/turns/save-turn') return reply(completed);
    if (path.endsWith('/events')) return route.fulfill({ status: 204 });
    if (path === operationPath + '/verify') {
      expect(route.request().method()).toBe('POST');
      checks++;
      if (checks === 1)
        return route.fulfill({
          status: 503,
          json: { code: 'VERIFICATION_PENDING', message: '暂时无法核实' },
        });
      return reply({ operation: resumes ? receipt : prepared, can_resume: !resumes });
    }
    if (path === operationPath + '/resume') {
      expect(route.request().method()).toBe('POST');
      resumes++;
      return reply({ queued: true, turn_id: failed.turn_id });
    }
    throw new Error(`Unexpected endpoint: ${path}`);
  });
  await page.goto('/showcase.html');
  const turn = page.locator('.turn');
  const verify = turn.getByRole('button', { name: '核实结果', exact: true });
  const resume = turn.getByRole('button', { name: '继续原操作', exact: true });
  await expect(turn).toContainText(prepared.operation_id);
  await expect(resume).toHaveCount(0);
  await verify.click();
  await expect(turn.getByRole('status')).toContainText('操作状态暂时无法核实');
  await expect(resume).toHaveCount(0);
  await verify.click();
  await expect(turn.getByRole('heading', { name: '已核实，尚未提交' })).toBeVisible();
  await resume.click();
  await expect(turn.getByRole('heading', { name: '已保存并核验' })).toBeVisible();
  await expect(resume).toHaveCount(0);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.reload();
  await expect(turn).toContainText(prepared.operation_id);
  await expect(turn.locator('.receipt-card')).toHaveCount(1);
  await verify.click();
  await expect(resume).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await expect.poll(() => checks).toBe(3);
  expect(resumes).toBe(1);
});
