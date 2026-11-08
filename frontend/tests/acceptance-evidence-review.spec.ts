import { expect, test } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';

test.skip(process.env.LIVE_ACCEPTANCE !== '1', 'Explicit acceptance opt-in required');
test.setTimeout(120_000);

test('review original evidence, duplicate original request, and capture loaded trace', async ({
  browser,
  baseURL,
}) => {
  const out = path.resolve(process.env.ACCEPTANCE_OUTPUT!);
  const read = (name: string) => JSON.parse(fs.readFileSync(path.join(out, name), 'utf8'));
  const write = (name: string, data: unknown) =>
    fs.writeFileSync(path.join(out, name), JSON.stringify(data, null, 2));
  const context = await browser.newContext({ baseURL, viewport: { width: 1440, height: 1100 } });
  const page = await context.newPage();
  page.setDefaultTimeout(20_000);
  await page.goto('/showcase.html');
  await page.getByRole('button', { name: /测试用户一/ }).click();
  await expect(page.getByRole('textbox', { name: '输入办理需求' })).toBeVisible();

  const rules = read('03-raw.json').result;
  await page.goto(`/showcase.html?session=${rules.session_id}`);
  await page.getByRole('button', { name: '查看办理记录' }).last().click();
  const trace = page.getByRole('region', { name: '请求执行轨迹' });
  await expect(trace).toBeVisible();
  await expect(page.getByText('正在读取完整执行链路…')).toHaveCount(0);
  await page.screenshot({ path: path.join(out, '03-run-evidence-loaded.png'), fullPage: true });
  await page.keyboard.press('Escape');

  const original = read('08-lost-response-raw.json');
  const postURL = `/api/v1/sessions/${original.result.session_id}/turns`;
  const repeated: any[] = [];
  for (let i = 0; i < 2; i++) {
    const response = await context.request.post(postURL, { data: original.request });
    expect(response.ok()).toBe(true);
    const result = await response.json();
    expect(result.turn_id).toBe(original.result.turn_id);
    repeated.push(result);
  }
  const current = await (
    await context.request.get(`/api/v1/turns/${original.result.turn_id}`)
  ).json();
  const operations = current.operations.filter((o: any) => o.state === 'committed');
  expect(operations).toHaveLength(1);
  expect(operations[0].before_revision).toBe(1);
  expect(operations[0].after_revision).toBe(2);
  expect(operations[0].after_values.purpose).toBe('办公耗材采购');
  expect(current.status).toBe('failed');
  expect(current.error.code).toBe('SUMMARY_INVALID');
  const runs = [];
  for (const run of current.runs)
    runs.push(await (await context.request.get(`/api/v1/runs/${run.run_id}`)).json());
  write('08-replay-evidence.json', {
    repeated,
    current,
    runs,
    finding:
      'Same request replays same failed turn; committed write occurs once, final receipt delivery failed.',
  });
  await page.goto(`/showcase.html?session=${current.session_id}`);
  const failed = page.locator('.turn').last();
  await expect(failed).toContainText('本轮未能完成');
  await failed.scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(out, '08-summary-failure-desktop.png'), fullPage: true });
  await failed.getByRole('button', { name: '核对已提交操作' }).click();
  await expect(page.getByRole('region', { name: '请求执行轨迹' })).toBeVisible();
  await page.locator('summary').filter({ hasText: '记忆、规则与业务依据' }).first().click();
  const receipts = page.getByRole('heading', { name: '请求级保存与核验' });
  await expect(receipts).toBeVisible();
  await receipts.scrollIntoViewIfNeeded();
  await page.screenshot({
    path: path.join(out, '08-committed-ledger-in-failed-request.png'),
    fullPage: true,
  });
  await page.keyboard.press('Escape');
  await page.setViewportSize({ width: 390, height: 844 });
  await failed.scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(out, '08-summary-failure-mobile.png'), fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1100 });

  const history = read('12a-raw.json').result;
  await page.goto(`/showcase.html?session=${history.session_id}`);
  const quote = page.locator('.turn').last();
  await expect(quote).toContainText('Q7421');
  await expect(quote).toContainText('R8532');
  await expect(quote).toContainText('我还没有选择');
  const citations = [];
  for (const card of history.final_cards.filter((c: any) => c.kind === 'history_quote')) {
    const evidence = await (
      await context.request.get(
        `/api/v1/evidence/${card.citation_id}?session_id=${card.session_id}`
      )
    ).json();
    citations.push(evidence);
  }
  expect(citations.map((c: any) => c.excerpt).join('')).toContain('Q7421');
  expect(citations.map((c: any) => c.excerpt).join('')).toContain('R8532');
  await page.screenshot({ path: path.join(out, '12a-verified-citation.png'), fullPage: true });
  write('12a-reviewed-evidence.json', {
    citations,
    finding:
      'Original assertion inspected citation metadata instead of source text. Source and rendered quote contain both unconfirmed candidates.',
  });
  const originalReport = read('report.json');
  const reviewed = {
    ...originalReport,
    review: 'agent evidence review; original report retained',
    records: originalReport.records.map((r: any) =>
      r.id === '12a'
        ? {
            ...r,
            original_status: r.status,
            status: 'pass',
            review_basis: '12a-reviewed-evidence.json and 12a-verified-citation.png',
          }
        : r
    ),
  };
  reviewed.records.find((r: any) => r.id === '08').review_basis =
    '08-replay-evidence.json: backend deduplication passes, final delivery fails; original network-error wording assertion also mismatched Failed to fetch.';
  write('reviewed-report.json', reviewed);
  await context.close();
});
