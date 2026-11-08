import { expect, test } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';

test.skip(process.env.LIVE_ACCEPTANCE !== '1', 'Requires explicit acceptance opt-in');
test.setTimeout(180_000);

test('semantic mode unavailable is reported as partial coverage in real browser', async ({
  browser,
  baseURL,
}) => {
  const out = path.resolve(process.env.ACCEPTANCE_OUTPUT!);
  const context = await browser.newContext({ baseURL, viewport: { width: 1440, height: 1100 } });
  const page = await context.newPage();
  page.setDefaultTimeout(15_000);
  await page.goto('/showcase.html');
  await page.getByRole('button', { name: /测试用户一/ }).click();
  await expect(page.getByRole('textbox', { name: '输入办理需求' })).toBeVisible();
  await page.getByRole('button', { name: '新对话', exact: true }).click();
  await expect(page.locator('.turn')).toHaveCount(0);
  const text =
    '请用语义搜索查找我以前说的Q7421柜子方案；如果语义索引未启用或覆盖不足，请明确说明限制，不要把检索不到说成从未说过。';
  await page.getByRole('textbox', { name: '输入办理需求' }).fill(text);
  const accepted = page.waitForResponse(
    (r) =>
      /\/sessions\/[^/]+\/turns$/.test(new URL(r.url()).pathname) && r.request().method() === 'POST'
  );
  await page.getByRole('button', { name: '发送消息' }).click();
  const first = await (await accepted).json();
  let result: any;
  await expect
    .poll(
      async () => {
        result = await (await context.request.get(`/api/v1/turns/${first.turn_id}`)).json();
        return result.status;
      },
      { timeout: 150_000, intervals: [1000] }
    )
    .not.toMatch(/^(queued|running)$/);
  const runs = [];
  for (const run of result.runs)
    runs.push(await (await context.request.get(`/api/v1/runs/${run.run_id}`)).json());
  await page.reload();
  await expect(page.locator('.turn').last()).toContainText(text);
  await page.locator('.turn').last().scrollIntoViewIfNeeded();
  await page.screenshot({
    path: path.join(out, '13-semantic-unavailable-desktop.png'),
    fullPage: true,
  });
  const outputs = runs
    .flatMap((r: any) => r.tools)
    .filter((t: any) => t.name === 'search_conversations')
    .map((t: any) => JSON.parse(t.actual_result));
  const partial = outputs.some(
    (o: any) => o.ok && o.data.status === 'partial' && o.data.coverage.complete === false
  );
  const passed =
    result.status === 'completed' &&
    partial &&
    /不足|未启用|不代表|限制/.test(result.final_message);
  fs.writeFileSync(
    path.join(out, '13-coverage-raw.json'),
    JSON.stringify(
      {
        text,
        result,
        runs,
        passed,
        limitation:
          'Tests disabled semantic mode degradation, not process crash or artificially damaged index.',
      },
      null,
      2
    )
  );
  const reviewedPath = path.join(out, 'reviewed-report.json');
  const reportPath = fs.existsSync(reviewedPath) ? reviewedPath : path.join(out, 'report.json');
  const report = JSON.parse(fs.readFileSync(reportPath, 'utf8'));
  const item = report.records.find((r: any) => r.id === '13');
  Object.assign(item, {
    status: passed ? 'pass' : 'fail',
    title: '语义未启用时如实反馈覆盖不足',
    original_status: 'not_run',
    review_basis: '13-coverage-raw.json; not an index corruption/crash test',
  });
  fs.writeFileSync(path.join(out, 'final-report.json'), JSON.stringify(report, null, 2));
  await context.close();
  expect(passed).toBe(true);
});
