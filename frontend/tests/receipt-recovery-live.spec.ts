import { expect, test } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';

test.skip(process.env.LIVE_ACCEPTANCE !== '1', 'Explicit real model acceptance opt-in required');
test.setTimeout(240_000);

test('real save receipt survives lost HTTP response and optional post-save summary fault', async ({
  browser,
  baseURL,
}) => {
  const fault = process.env.EXPECT_RECEIPT_DEGRADATION === '1';
  const out = path.resolve(process.env.ACCEPTANCE_OUTPUT!);
  fs.mkdirSync(out, { recursive: false });
  const context = await browser.newContext({ baseURL, viewport: { width: 1440, height: 1100 } });
  const page = await context.newPage();
  page.setDefaultTimeout(15_000);
  const write = (name: string, value: unknown) =>
    fs.writeFileSync(path.join(out, name), JSON.stringify(value, null, 2));
  async function terminal(id: string) {
    let r: any;
    await expect
      .poll(
        async () => {
          r = await (await context.request.get(`/api/v1/turns/${id}`)).json();
          return r.status;
        },
        { timeout: 150_000, intervals: [1000] }
      )
      .not.toMatch(/^(running|queued)$/);
    return r;
  }
  await page.goto('/showcase.html');
  await page.getByRole('button', { name: /测试用户一/ }).click();
  const input = page.getByRole('textbox', { name: '输入办理需求' });
  await expect(input).toBeVisible();
  await page.getByRole('button', { name: '新对话', exact: true }).click();
  await expect(page.locator('.turn')).toHaveCount(0);
  await input.fill('检查甲企业2026年9月发票用途，列出待补项和依据');
  const checked = page.waitForResponse(
    (r) =>
      /\/sessions\/[^/]+\/turns$/.test(new URL(r.url()).pathname) && r.request().method() === 'POST'
  );
  await page.getByRole('button', { name: '发送消息' }).click();
  const checkTurn = await terminal((await (await checked).json()).turn_id);
  write('before-check.json', checkTurn);
  expect(checkTurn.status).toMatch(/^(completed|waiting_user)$/);
  expect(checkTurn.final_cards.find((c: any) => c.kind === 'purpose_check').missing_count).toBe(5);
  await page.reload();
  await expect(page.locator('.turn').last()).toContainText('缺少用途5张');
  let original: any,
    result: any,
    postURL = '';
  await page.route(
    '**/api/v1/sessions/*/turns',
    async (route) => {
      original = route.request().postDataJSON();
      postURL = route.request().url();
      const response = await route.fetch();
      result = await terminal((await response.json()).turn_id);
      write('original-save.json', { fault_injection: fault, request: original, result });
      await route.abort('connectionfailed');
    },
    { times: 1 }
  );
  await input.fill('把甲企业F002的用途填写为办公耗材采购');
  await page.getByRole('button', { name: '发送消息' }).click();
  await expect(page.getByRole('alert')).toContainText('暂时无法确认服务端处理状态', {
    timeout: 150_000,
  });
  await page.screenshot({ path: path.join(out, 'response-lost.png'), fullPage: true });
  const repeated = page.waitForResponse(
    (r) => r.url() === postURL && r.request().method() === 'POST'
  );
  await page.getByRole('button', { name: '核对原请求', exact: true }).click();
  const response = await repeated;
  expect(response.request().postDataJSON().client_request_id).toBe(original.client_request_id);
  expect((await response.json()).turn_id).toBe(result.turn_id);
  await page.reload();
  const turn = page.locator('.turn').last();
  await expect(turn.getByRole('heading', { name: '已保存并核验', exact: true })).toBeVisible();
  await turn.scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(out, 'receipt-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.reload();
  await page.locator('.receipt-card').last().scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(out, 'receipt-mobile.png'), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.setViewportSize({ width: 1440, height: 1100 });
  const runs = [];
  for (const run of result.runs)
    runs.push(await (await context.request.get(`/api/v1/runs/${run.run_id}`)).json());
  const replay = await context.request.post(postURL, { data: original });
  expect((await replay.json()).turn_id).toBe(result.turn_id);
  const final = await terminal(result.turn_id);
  const ops = final.operations.filter((o: any) => o.state === 'committed');
  expect(ops).toHaveLength(1);
  expect(ops[0].before_revision).toBe(1);
  expect(ops[0].after_revision).toBe(2);
  write('verified-evidence.json', {
    fault_injection: fault,
    result: final,
    runs,
    duplicate_write: false,
    same_request_id: true,
  });
  if (fault) {
    expect(final.status).toBe('failed');
    expect(final.error.code).toBe('SUMMARY_INVALID');
    expect(final.final_message).toContain('不表示本轮其他事项已完成');
    await expect(page.getByText('保存回执已交付，后续未完成', { exact: true })).toBeVisible();
  } else expect(final.status).toBe('completed');
  await context.close();
});
