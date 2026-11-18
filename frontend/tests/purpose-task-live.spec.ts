import { expect, test } from '@playwright/test';

test.skip(
  process.env.PURPOSE_TASK_E2E !== '1',
  'Requires a fresh isolated database and a real model'
);
test.setTimeout(300_000);

test('clarifies, saves, corrects, cancels and verifies a purpose task', async ({
  page,
}, testInfo) => {
  const injectLoss = process.env.PURPOSE_TASK_RECOVERY_E2E === '1';
  await page.goto('/showcase.html');
  await page.getByRole('button', { name: /测试用户一/ }).click();
  const input = page.getByRole('textbox', { name: '输入办理需求' });
  await expect(input).toBeVisible();
  const currentSession = () =>
    page.evaluate(() => localStorage.getItem('tax-harness:session:U001'));
  await expect.poll(currentSession).not.toBeNull();
  const created = page.waitForResponse(
    (r) =>
      new URL(r.url()).pathname === '/api/v1/sessions' &&
      r.request().method() === 'POST' &&
      r.status() === 201
  );
  await page.getByRole('button', { name: '新对话', exact: true }).click();
  const { session_id: sessionId } = await (await created).json();
  await expect.poll(currentSession).toBe(sessionId);
  await page.getByLabel('当前企业').selectOption('C001');
  await page.getByLabel('当前 Agent').selectOption({ label: '财税办理助手' });

  async function terminal(id: string, label: string) {
    let turn: any;
    await expect
      .poll(
        async () => {
          turn = await (await page.request.get('/api/v1/turns/' + id)).json();
          return turn.status;
        },
        { timeout: 75_000 }
      )
      .not.toMatch(/^(queued|running)$/);
    const runs = await Promise.all(
      turn.runs.map(async ({ run_id }: { run_id: string }) =>
        (await page.request.get('/api/v1/runs/' + run_id)).json()
      )
    );
    await testInfo.attach(label, {
      body: JSON.stringify({ turn, runs }, null, 2),
      contentType: 'application/json',
    });
    return turn;
  }

  async function losePost(pathname: string) {
    let first: { body: unknown; turnId: string } | undefined;
    await page.route(
      '**' + pathname,
      async (route) => {
        const stored = await route.fetch();
        first = { body: route.request().postDataJSON(), turnId: (await stored.json()).turn_id };
        await route.abort('connectionfailed');
      },
      { times: 1 }
    );
    return async (response: import('@playwright/test').Response) => {
      expect(first?.turnId).toBeTruthy();
      expect(response.request().postDataJSON()).toEqual(first?.body);
      expect((await response.json()).turn_id).toBe(first?.turnId);
      await testInfo.attach('replayed request', {
        body: JSON.stringify({ pathname, first }, null, 2),
        contentType: 'application/json',
      });
    };
  }

  async function send(message: string, label: string, lose = false) {
    const pathname = '/api/v1/sessions/' + sessionId + '/turns';
    const replayed = lose ? await losePost(pathname) : null;
    const accepted = page.waitForResponse(
      (r) => new URL(r.url()).pathname === pathname && r.request().method() === 'POST'
    );
    await input.fill(message);
    await page.getByRole('button', { name: '发送消息' }).click();
    if (replayed) await page.getByRole('button', { name: '核对原请求', exact: true }).click();
    const response = await accepted;
    expect(response.ok(), await response.text()).toBe(true);
    if (replayed) await replayed(response);
    return terminal((await response.json()).turn_id, label);
  }

  async function save(message: string, value: string, label: string, lose = false) {
    let turn = await send(message, label);
    const plan = turn.final_cards.find((c: any) => c.kind === 'write_plan');
    if (lose) expect(plan).toBeTruthy();
    if (plan) {
      expect(plan.invoice_id).toBe('F002');
      const path = '/api/v1/write-plans/' + plan.plan_id;
      // Keep the card on its last known state until the user retries the lost POST.
      if (lose) await page.route('**' + path, (route) => route.fulfill({ json: plan }));
      const replayed = lose ? await losePost(path + '/confirm') : null;
      const confirm = page.waitForResponse((r) => new URL(r.url()).pathname === path + '/confirm');
      const current = page.locator('.turn').filter({ hasText: message });
      await current.getByRole('button', { name: '确认并保存', exact: true }).click();
      if (replayed)
        await current.getByRole('button', { name: '核对确认结果', exact: true }).click();
      const accepted = await confirm;
      expect(accepted.ok(), await accepted.text()).toBe(true);
      if (replayed) {
        await replayed(accepted);
        await page.unroute('**' + path);
      }
      turn = await terminal((await accepted.json()).turn_id, label + ' confirmation');
    }
    const receipt = turn.final_cards.find((c: any) => c.kind === 'operation');
    expect(receipt, turn.final_message).toMatchObject({
      invoice_id: 'F002',
      commit_state: 'committed',
      verification_state: 'verified',
      saved_values: { purpose: value },
    });
    expect(turn.final_cards.find((c: any) => c.kind === 'purpose_check')).toMatchObject({
      missing_count: 1,
      rows: [{ invoice_id: 'F003' }],
    });
    return receipt;
  }

  const first = await send('帮我检查一下甲企业哪些发票还没填用途。', 'T1 missing period');
  expect(first.status, first.final_message).toBe('waiting_user');
  expect(first.pending_clarification.missing_fields).toEqual(['period']);
  const checked = await send('2026 年 9 月。', 'T2 current task', injectLoss);
  expect(checked.continuation_of_turn_id, checked.final_message).toBe(first.turn_id);
  expect(checked.final_cards.find((c: any) => c.kind === 'purpose_check')).toMatchObject({
    missing_count: 2,
    rows: [{ invoice_id: 'F002' }, { invoice_id: 'F003' }],
  });
  const initial = await save(
    '把甲企业 F002 的用途填写为办公耗材采购。F003 先别动。',
    '办公耗材采购',
    'T3 partial save',
    injectLoss
  );
  const corrected = await save(
    '更正甲企业 F002 的用途为会议物料采购。F003 先别动。',
    '会议物料采购',
    'T4 correction'
  );
  expect(corrected.operation_id).not.toBe(initial.operation_id);
  expect(corrected.before_revision).toBe(initial.after_revision);
  await page.getByText('已保存并核验', { exact: true }).last().waitFor();
  await page.screenshot({ path: testInfo.outputPath('corrected.png'), fullPage: true });

  const cancelled = await send(
    '取消这次甲企业 2026 年 9 月的用途补充任务。剩下的发票不办了。',
    'T5 cancellation'
  );
  expect(cancelled.final_message).toMatch(/待办已取消/);
  expect(cancelled.final_message).toContain('F003');
  const history = await send('这次保存了什么？还有没有待办？', 'T6 receipts and status');
  expect(
    history.final_cards.filter((c: any) => c.kind === 'operation').map((c: any) => c.operation_id)
  ).toEqual([initial.operation_id, corrected.operation_id]);
  const tasks = await (await page.request.get('/api/v1/tasks?company_id=C001')).json();
  expect(tasks).toHaveLength(1);
  expect(tasks[0]).toMatchObject({
    task_id: first.pending_clarification.task_id,
    status: 'cancelled',
  });
  await expect(page.locator('.task-strip')).toHaveCount(0);
  await expect(page.locator('.turn').last()).toContainText('待办已取消');
  await page.screenshot({ path: testInfo.outputPath('cancelled.png'), fullPage: true });
});
