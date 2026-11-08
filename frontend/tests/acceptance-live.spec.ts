import { expect, test, type Page, type BrowserContext } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';

// Opt-in only: sends simulated competition data to the configured real model.
test.skip(process.env.LIVE_ACCEPTANCE !== '1', 'Requires explicit live acceptance opt-in');
test.setTimeout(900_000);

test('real-model browser acceptance with screenshots and raw receipts', async ({
  browser,
  baseURL,
}) => {
  const out = path.resolve(
    process.env.ACCEPTANCE_OUTPUT ?? '../.local/evidence/browser-acceptance-20261107-01'
  );
  fs.mkdirSync(out, { recursive: false });
  const records: any[] = [];
  const pageErrors: string[] = [];
  const context = await browser.newContext({ baseURL, viewport: { width: 1440, height: 1100 } });
  const page = await context.newPage();
  page.setDefaultTimeout(15_000);
  page.on('pageerror', (e) => pageErrors.push(e.message));
  const write = (name: string, data: unknown) =>
    fs.writeFileSync(path.join(out, name), JSON.stringify(data, null, 2));
  const report = () =>
    write('report.json', {
      mode: 'real-model-browser',
      database: 'tax_harness_test',
      human_signoff: 'pending',
      pageErrors,
      records,
    });

  async function fresh(company: string = 'C001') {
    await page.getByRole('button', { name: '新对话', exact: true }).click();
    await expect(page.locator('.turn')).toHaveCount(0);
    if ((await page.getByLabel('当前企业').inputValue()) !== company)
      await page.getByLabel('当前企业').selectOption(company);
    await expect(page.getByLabel('当前企业')).toHaveValue(company);
    await expect(page.getByRole('textbox', { name: '输入办理需求' })).toBeEnabled();
  }
  async function terminal(id: string, client: BrowserContext = context) {
    let result: any;
    await expect
      .poll(
        async () => {
          const response = await client.request.get(`/api/v1/turns/${id}`);
          if (!response.ok()) return 'unreadable';
          result = await response.json();
          return result.status;
        },
        { timeout: 150_000, intervals: [500, 1000, 1500] }
      )
      .not.toMatch(/^(queued|running|unreadable)$/);
    return result;
  }
  async function send(text: string, id: string) {
    const input = page.getByRole('textbox', { name: '输入办理需求' });
    await input.fill(text);
    const response = page.waitForResponse(
      (r) =>
        /\/sessions\/[^/]+\/turns$/.test(new URL(r.url()).pathname) &&
        r.request().method() === 'POST'
    );
    await page.getByRole('button', { name: '发送消息' }).click();
    const acceptedResponse = await response;
    expect(acceptedResponse.ok()).toBe(true);
    const accepted = await acceptedResponse.json();
    const result = await terminal(accepted.turn_id);
    const runs = [];
    for (const run of result.runs ?? []) {
      const r = await context.request.get(`/api/v1/runs/${run.run_id}`);
      runs.push(await r.json());
    }
    write(`${id}-raw.json`, { input: text, result, runs });
    await page.reload();
    await expect(page.locator('.turn').last()).toContainText(text);
    expect(result.status).toMatch(/^(completed|waiting_user)$/);
    return { result, runs };
  }
  async function shots(id: string, mobile = false) {
    const turn = page.locator('.turn').last();
    await turn.scrollIntoViewIfNeeded();
    await page.screenshot({
      path: path.join(out, `${id}-desktop.png`),
      fullPage: true,
      animations: 'disabled',
    });
    const cards = turn.locator('.result-card');
    for (let i = 0; i < (await cards.count()); i++) {
      await cards
        .nth(i)
        .screenshot({ path: path.join(out, `${id}-card-${i + 1}.png`), animations: 'disabled' });
    }
    if (mobile) {
      await page.setViewportSize({ width: 390, height: 844 });
      await page.reload();
      const last = page.locator('.turn').last();
      await expect(last).toBeVisible();
      const progress = last.locator('.request-progress');
      if (await progress.count()) await progress.scrollIntoViewIfNeeded();
      else await last.scrollIntoViewIfNeeded();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(
        true
      );
      await page.screenshot({
        path: path.join(out, `${id}-mobile.png`),
        fullPage: true,
        animations: 'disabled',
      });
      await page.setViewportSize({ width: 1440, height: 1100 });
    }
  }
  async function check(id: string, title: string, action: () => Promise<unknown>) {
    try {
      const details = await action();
      records.push({ id, title, status: 'pass', details });
    } catch (e) {
      await page
        .screenshot({ path: path.join(out, `${id}-failure.png`), fullPage: true })
        .catch(() => {});
      records.push({ id, title, status: 'fail', error: String(e) });
    }
    report();
    console.log(`${id}: ${records.at(-1).status}`);
  }
  const progress = (r: any) =>
    r.final_cards.filter((c: any) => c.kind === 'request_progress').flatMap((c: any) => c.items);
  const scopes = (r: any) =>
    r.final_cards
      .filter((c: any) => c.kind === 'monthly_summary')
      .map((c: any) => `${c.company_id}/${c.period}`)
      .sort();
  const tools = (runs: any[]) => runs.flatMap((r: any) => r.tools ?? []);

  await page.goto('/showcase.html');
  await page.getByRole('button', { name: /测试用户一/ }).click();
  await expect(page.getByRole('textbox', { name: '输入办理需求' })).toBeVisible();
  const me = await (await context.request.get('/api/v1/me')).json();
  expect(me.model_mode).toBe('live');
  write('environment.json', {
    url: baseURL,
    model_mode: me.model_mode,
    database: 'tax_harness_test',
    viewport: { width: 1440, height: 1100 },
  });

  await check('01a', '无企业无月份时只问缺失范围', async () => {
    await fresh('');
    const { result, runs } = await send('查询月度发票总额', '01a');
    await shots('01a');
    expect(result.status).toBe('waiting_user');
    expect(tools(runs).filter((t: any) => t.name === 'query_month')).toHaveLength(0);
    return { turn_id: result.turn_id, message: result.final_message };
  });
  await check('01b', '已选企业但缺月份时不猜月份', async () => {
    await fresh();
    const { result } = await send('查询甲企业月度发票总额', '01b');
    await shots('01b');
    expect(result.status).toBe('waiting_user');
    expect(result.pending_clarification.missing_fields).toContain('period');
    return { turn_id: result.turn_id };
  });
  await check('02', '双月份查询完整交付并刷新和手机查看', async () => {
    await fresh();
    const { result } = await send('查询甲企业2026年8月和9月发票总额', '02');
    await shots('02', true);
    expect(result.status).toBe('completed');
    expect(scopes(result)).toEqual(['C001/2026-08', 'C001/2026-09']);
    expect(progress(result).every((i: any) => i.status === 'verified')).toBe(true);
    return { turn_id: result.turn_id };
  });
  await check('03', '查询并解释规则，两项均有依据', async () => {
    await fresh();
    const { result } = await send('查询甲企业2026年9月发票总额，并解释适用规则', '03');
    await shots('03');
    expect(
      progress(result)
        .map((i: any) => i.tool)
        .sort()
    ).toEqual(['get_rules', 'query_month']);
    expect(result.final_cards.some((c: any) => c.kind === 'rules')).toBe(true);
    await page.getByRole('button', { name: '查看办理记录' }).last().click();
    await expect(page.getByRole('dialog')).toBeVisible();
    await expect(page.getByRole('region', { name: '请求执行轨迹' })).toBeVisible();
    await page.screenshot({ path: path.join(out, '03-run-evidence.png'), fullPage: true });
    await page.keyboard.press('Escape');
    return { turn_id: result.turn_id };
  });
  await check('04', '双企业与月份配对不串范围', async () => {
    await fresh();
    const { result, runs } = await send(
      '查询甲企业2026年8月发票总额；查询乙企业2026年9月发票总额',
      '04'
    );
    await shots('04', true);
    expect(scopes(result)).toEqual(['C001/2026-08', 'C002/2026-09']);
    expect(tools(runs).filter((t: any) => t.name === 'query_month')).toHaveLength(2);
    return { turn_id: result.turn_id };
  });
  await check('05', '不支持月份明确反馈无法执行', async () => {
    await fresh();
    const { result } = await send('检查甲企业2026年8月发票用途', '05');
    await shots('05', true);
    expect(progress(result)[0].status).toBe('blocked');
    await expect(page.getByText('已反馈，含未完成事项', { exact: true })).toBeVisible();
    return { turn_id: result.turn_id };
  });
  await check('06a', '否定的八月查询不执行', async () => {
    await fresh();
    const { result, runs } = await send(
      '不要查询甲企业2026年8月发票总额；查询甲企业2026年9月发票总额',
      '06a'
    );
    await shots('06a');
    expect(scopes(result)).toEqual(['C001/2026-09']);
    expect(
      tools(runs).some((t: any) => t.name === 'query_month' && t.arguments.period === '2026-08')
    ).toBe(false);
    return { turn_id: result.turn_id };
  });
  await check('06b', '引用中的查询指令不执行', async () => {
    await fresh();
    const { result, runs } = await send(
      '仅记录一句示例，不执行查询：“查询甲企业2026年8月发票总额”。',
      '06b'
    );
    await shots('06b');
    expect(tools(runs).some((t: any) => t.name === 'query_month')).toBe(false);
    return { turn_id: result.turn_id };
  });
  await check('07', '用途检查为保存提供真实待补列表', async () => {
    await fresh();
    const { result } = await send('检查甲企业2026年9月发票用途，列出待补项和依据', '07');
    await shots('07');
    const card = result.final_cards.find((c: any) => c.kind === 'purpose_check');
    expect(card.missing_count).toBe(5);
    expect(card.rows.some((r: any) => r.invoice_id === 'F002')).toBe(true);
    return { turn_id: result.turn_id, missing_count: card.missing_count };
  });
  await check('08', '提交已完成但响应丢失，重试同一请求只有一次操作', async () => {
    const input = page.getByRole('textbox', { name: '输入办理需求' });
    const text = '把甲企业F002的用途填写为办公耗材采购';
    let original: any;
    let lostResult: any;
    let postURL = '';
    await page.route(
      '**/api/v1/sessions/*/turns',
      async (route) => {
        original = route.request().postDataJSON();
        postURL = route.request().url();
        const response = await route.fetch();
        const accepted = await response.json();
        lostResult = await terminal(accepted.turn_id);
        write('08-lost-response-raw.json', { request: original, result: lostResult });
        await route.abort('connectionfailed');
      },
      { times: 1 }
    );
    await input.fill(text);
    await page.getByRole('button', { name: '发送消息' }).click();
    await expect(page.getByRole('alert')).toContainText('暂时无法确认服务端处理状态', {
      timeout: 150_000,
    });
    await page.screenshot({ path: path.join(out, '08-response-lost.png'), fullPage: true });
    expect(
      lostResult.status,
      'Committed writes must still deliver their receipt; inspect retained raw operations'
    ).toBe('completed');
    const repeat = page.waitForResponse(
      (r) => r.url() === postURL && r.request().method() === 'POST'
    );
    await page.getByRole('button', { name: '发送消息' }).click();
    const response = await repeat;
    expect(response.request().postDataJSON().client_request_id).toBe(original.client_request_id);
    const accepted = await response.json();
    expect(accepted.turn_id).toBe(lostResult.turn_id);
    const result = await terminal(accepted.turn_id);
    await page.reload();
    await expect(page.getByRole('heading', { name: '已保存并核验', exact: true })).toBeVisible();
    await shots('08', true);
    const operations = result.operations.filter(
      (o: any) => o.invoice_id === 'F002' && o.state === 'committed'
    );
    expect(operations).toHaveLength(1);
    expect(operations[0].after_values.purpose).toBe('办公耗材采购');
    const again = await context.request.post(postURL, { data: original });
    expect((await again.json()).turn_id).toBe(result.turn_id);
    const final = await terminal(result.turn_id);
    expect(final.operations.filter((o: any) => o.state === 'committed')).toHaveLength(1);
    write('08-final-receipt.json', { original_request: original, result: final });
    return {
      turn_id: result.turn_id,
      operation_id: operations[0].operation_id,
      client_request_id: original.client_request_id,
    };
  });
  await check('09', '新会话续办重查真实剩余项', async () => {
    await fresh();
    const { result, runs } = await send('继续处理甲企业的未完成任务，先核对最新状态', '09');
    await shots('09');
    expect(tools(runs).some((t: any) => t.name === 'check_purposes')).toBe(true);
    const card = result.final_cards.find((c: any) => c.kind === 'purpose_check');
    expect(card.missing_count).toBe(4);
    expect(card.rows.some((r: any) => r.invoice_id === 'F002')).toBe(false);
    return { turn_id: result.turn_id, missing_count: card.missing_count };
  });
  await check('10a', '显式长期偏好保存', async () => {
    await fresh();
    const { result } = await send('以后回答请简洁，请记住这个长期偏好', '10a');
    await shots('10a');
    const preferences = await (await context.request.get('/api/v1/preferences')).json();
    write('10a-preferences.json', preferences);
    expect(preferences.some((p: any) => p.value.includes('简洁'))).toBe(true);
    return { turn_id: result.turn_id };
  });
  await check('10b', '临时表达要求不覆盖长期偏好', async () => {
    const before = await (await context.request.get('/api/v1/preferences')).json();
    const { result, runs } = await send(
      '只在这一次详细说明，不要记住或更改长期偏好。查询甲企业2026年9月发票总额',
      '10b'
    );
    await shots('10b');
    const after = await (await context.request.get('/api/v1/preferences')).json();
    expect(after.map((p: any) => [p.preference_id, p.value, p.revision])).toEqual(
      before.map((p: any) => [p.preference_id, p.value, p.revision])
    );
    expect(
      tools(runs).some((t: any) => t.name === 'manage_preference' && t.arguments.action !== 'list')
    ).toBe(false);
    return { turn_id: result.turn_id };
  });
  await check('10c', '更正长期偏好', async () => {
    const { result } = await send('把刚才的长期回复偏好更正为详细说明，并记住', '10c');
    await shots('10c');
    const preferences = await (await context.request.get('/api/v1/preferences')).json();
    expect(preferences.some((p: any) => p.value.includes('详细'))).toBe(true);
    expect(preferences.some((p: any) => p.value === '简洁')).toBe(false);
    return { turn_id: result.turn_id };
  });
  await check('10d', '删除测试长期偏好', async () => {
    const { result } = await send('删除刚才记住的长期回复偏好，以后不要再使用', '10d');
    await shots('10d');
    const preferences = await (await context.request.get('/api/v1/preferences')).json();
    expect(preferences.filter((p: any) => p.stable_key === 'response_style')).toHaveLength(0);
    return { turn_id: result.turn_id };
  });
  await check('11a', '只读用户切换后隐藏原用户对话', async () => {
    await page.getByLabel('当前模拟用户').selectOption('U002');
    await expect(page.locator('.turn')).toHaveCount(0);
    await fresh();
    const { result } = await send('查询甲企业2026年9月发票总额', '11a');
    await shots('11a');
    expect(scopes(result)).toEqual(['C001/2026-09']);
    return { turn_id: result.turn_id };
  });
  await check('11b', '只读用户不能保存用途', async () => {
    const { result } = await send('把甲企业F003的用途填写为测试越权修改', '11b');
    await shots('11b');
    expect(result.operations.filter((o: any) => o.state === 'committed')).toHaveLength(0);
    expect(result.final_message).toMatch(/无权|权限|不能修改|只读|查看/);
    return { turn_id: result.turn_id };
  });
  await check('11c', '未授权企业不能查询', async () => {
    const { result } = await send('查询乙企业2026年9月发票总额', '11c');
    await shots('11c');
    expect(scopes(result)).toHaveLength(0);
    expect(result.final_message).toMatch(/无权|权限/);
    return { turn_id: result.turn_id };
  });
  await check('12a', '旧聊天多个候选不擅自代替用户选择', async () => {
    await page.getByLabel('当前模拟用户').selectOption('U001');
    await fresh();
    await send(
      '仅记录两个未确认候选：方案A资料放东侧柜Q7421；方案B资料放西侧柜R8532。我还没有选择，不授权修改。',
      '12a-seed'
    );
    await fresh();
    const { result, runs } = await send(
      '查找我刚才在另一个会话记录的两个未确认候选方案，引用原话；不要替我选择',
      '12a'
    );
    await shots('12a');
    expect(tools(runs).some((t: any) => t.name === 'search_conversations')).toBe(true);
    const quotes = result.final_cards.filter((c: any) => c.kind === 'history_quote');
    expect(quotes.length).toBeGreaterThan(0);
    const evidence = [];
    for (const card of quotes) {
      evidence.push(
        await (
          await context.request.get(
            `/api/v1/evidence/${card.citation_id}?session_id=${card.session_id}`
          )
        ).json()
      );
    }
    expect(evidence.map((e: any) => e.excerpt).join('')).toContain('Q7421');
    expect(evidence.map((e: any) => e.excerpt).join('')).toContain('R8532');
    await expect(page.locator('.turn').last()).toContainText('Q7421');
    await expect(page.locator('.turn').last()).toContainText('R8532');
    expect(result.operations.filter((o: any) => o.state === 'committed')).toHaveLength(0);
    return { turn_id: result.turn_id };
  });
  await check('12b', '旧聊天无匹配不编造原话', async () => {
    await fresh();
    const { result, runs } = await send(
      '搜索我过去说过的ZX-NO-MATCH-1107火星报税柜，找不到就说明未找到，不要编造',
      '12b'
    );
    await shots('12b');
    expect(tools(runs).some((t: any) => t.name === 'search_conversations')).toBe(true);
    expect(result.final_cards.some((c: any) => c.kind === 'history_quote')).toBe(false);
    expect(result.final_message).toMatch(/未找到|没有找到|不足|没有匹配/);
    return { turn_id: result.turn_id };
  });
  records.push({
    id: '13',
    title: '索引覆盖不足故障注入',
    status: 'not_run',
    reason: '未人为破坏本轮历史索引；不将自然无匹配冒充覆盖不足验证。',
  });
  report();
  await context.close();
  expect(pageErrors).toEqual([]);
  expect(
    records.filter((r) => r.status === 'fail').map((r) => r.id),
    'See retained raw evidence and failure screenshots'
  ).toEqual([]);
});
