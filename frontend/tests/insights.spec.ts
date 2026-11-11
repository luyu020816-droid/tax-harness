import { expect, test } from '@playwright/test';

test('insights: scoped history, chain, unknown usage, refresh, comparison and responsive layout', async ({
  page,
}, info) => {
  const id = '11111111-1111-4111-8111-111111111111';
  const cost = {
    known_cost: '0.002',
    complete_cost: null,
    unknown_calls: 1,
    currency: 'USD',
    pricing_versions: ['2026-10-31'],
    upper_estimate_calls: 0,
  };
  const request = {
    turn_id: id,
    text: '查询甲企业月度资料（观测夹具）',
    status: 'completed',
    created_at: '2026-11-02T01:00:00Z',
    run_count: 2,
    model_attempts: 2,
    usage_known_calls: 1,
    unknown_dispatch_calls: 0,
    input_tokens: 1000,
    output_tokens: 200,
    active_ms: 5000,
    elapsed_ms: 15000,
    queue_ms: 100,
    models: ['test-model'],
    cost,
    content_available: true,
  };
  const errors: string[] = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await page.route('**/api/v1/**', async (route) => {
    const p = new URL(route.request().url()).pathname.replace('/api/v1', '');
    const reply = (json: unknown) => route.fulfill({ json });
    if (p === '/me')
      return reply({
        user_id: 'U001',
        display_name: '测试用户一',
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
        model_ready: true,
        model_mode: 'test',
        dataset_profile: 'competition',
        invoice_import_ready: false,
      });
    if (p === '/sessions')
      return reply([
        {
          session_id: 's1',
          selected_company_id: 'C001',
          title: '测试会话',
          updated_at: '2026-11-02T00:00:00Z',
        },
      ]);
    if (p.endsWith('/messages')) return reply({ turns: [], has_more: false });
    if (p === '/tasks') return reply([]);
    if (p.endsWith('/context-usage'))
      return reply({ status: 'no_calls', input_tokens: 0, effective_window: 100000 });
    if (p === '/insights/summary')
      return reply({
        request_count: 1,
        terminal_count: 1,
        completion_ratio: 1,
        p50_ms: 5000,
        p95_ms: 5000,
        timed_requests: 1,
        small_sample: true,
        input_tokens: 1000,
        output_tokens: 200,
        model_attempts: 2,
        run_count: 2,
        states: { completed: 1 },
        cost,
        measured_at: '2026-11-02T01:01:00Z',
        coverage: {
          usage_known: 1,
          dispatched: 2,
          dispatch_unknown: 0,
          timed_calls: 2,
          versioned_runs: 2,
          runs: 2,
        },
      });
    if (p === '/insights/requests') {
      const params = new URL(route.request().url()).searchParams;
      expect(params.get('limit')).toBe('5');
      const last = params.has('cursor');
      return reply({
        requests: last
          ? [{ ...request, text: '末页记录' }]
          : Array.from({ length: 5 }, (_, i) => ({
              ...request,
              turn_id: i === 0 ? id : `request-${i}`,
              text: i === 0 ? request.text : `历史请求 ${i}`,
            })),
        has_more: !last,
        next_cursor: last ? null : 'page-two',
      });
    }
    if (p === `/insights/requests/${id}`)
      return reply({
        request,
        runs: [
          {
            run_id: 'run1',
            generation: 1,
            state: 'failed',
            started_at: '2026-11-02T01:00:00Z',
            finished_at: '2026-11-02T01:00:02Z',
            content_available: true,
            insight_snapshot: { schema_version: 1, model: 'test-model' },
            has_more_spans: false,
            evidence: { contexts: [], operations: [] },
            spans: [
              {
                span_id: id,
                kind: 'model',
                name: '生成答复',
                status: 'failed',
                started_at: '2026-11-02T01:00:00Z',
                duration_ms: 2000,
                measurement_kind: 'measured',
                error_code: 'MODEL_UNAVAILABLE',
                attributes: { model: 'test-model', usage: null },
              },
              ...[
                ['scope-block', 'NEEDS_SCOPE_CLARIFICATION'],
                ['access-block', 'ACCESS_DENIED'],
              ].map(([span_id, error_code]) => ({
                span_id,
                kind: 'tool',
                name: 'query_month',
                status: 'failed',
                started_at: '2026-11-02T01:00:02Z',
                duration_ms: 27.5,
                measurement_kind: 'measured',
                error_code,
                attributes: {},
              })),
            ],
          },
          {
            run_id: 'run2',
            generation: 2,
            state: 'completed',
            started_at: '2026-11-02T01:00:12Z',
            finished_at: '2026-11-02T01:00:15Z',
            content_available: true,
            insight_snapshot: null,
            has_more_spans: false,
            evidence: { contexts: [], operations: [] },
            spans: [
              {
                span_id: 'retrieval-stage',
                kind: 'retrieval',
                name: '聊天融合与支持度',
                status: 'succeeded',
                started_at: '2026-11-02T01:00:12Z',
                duration_ms: 10,
                measurement_kind: 'measured',
                attributes: {
                  retrieval: {
                    profile_id: 'profile-fixture-123456789',
                    policy_hash: 'policy-fixture-987654321',
                    candidate_count: 6,
                    accepted_count: 3,
                    coverage: { eligible: 10, indexed: 8, pending: 2 },
                    local_tokens: null,
                  },
                },
              },
              {
                span_id: 'second',
                kind: 'model',
                name: '恢复答复',
                status: 'succeeded',
                started_at: '2026-11-02T01:00:12Z',
                duration_ms: 3000,
                measurement_kind: 'measured',
                attributes: {
                  model: 'test-model',
                  usage: { prompt_tokens: 1000, completion_tokens: 200 },
                },
              },
            ],
          },
        ],
      });
    if (p.startsWith('/insights/spans/'))
      return reply({
        redacted: false,
        text: '{"fixture":true}',
        next_offset: null,
        total_bytes: 16,
      });
    if (p === '/insights/evaluations') return reply([]);
    throw new Error(`Unmocked endpoint ${p}`);
  });
  await page.goto('/showcase.html');
  await page.getByRole('button', { name: '运行洞察', exact: true }).click();
  await expect(page.getByRole('heading', { name: '运行洞察', exact: true })).toBeVisible();
  await expect(page.locator('.insight-kpis')).toContainText('1,200');
  await expect(page.locator('.insight-history-row')).toHaveCount(5);
  const rowHeight = await page
    .locator('.insight-history-row')
    .first()
    .evaluate((e) => e.getBoundingClientRect().height);
  await page.getByRole('button', { name: '下一页', exact: true }).click();
  await expect(page.locator('.insight-history-row')).toHaveCount(1);
  expect(
    await page
      .locator('.insight-history-row')
      .first()
      .evaluate((e) => e.getBoundingClientRect().height)
  ).toBe(rowHeight);
  await expect(page.getByRole('button', { name: '下一页', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: '上一页', exact: true }).click();
  await expect(page.locator('.insight-history-row')).toHaveCount(5);
  await page.getByRole('button', { name: /查询甲企业月度资料（观测夹具）/ }).click();
  await expect(page.getByRole('region', { name: '请求执行轨迹' })).toContainText('恢复 / 重试');
  const scope = page.getByRole('button', { name: /query_month.*需确认范围/ });
  await expect(scope).toBeVisible();
  await expect(scope.locator('..')).toContainText('请确认企业或年月');
  await expect(page.getByRole('button', { name: /query_month.*无权限/ })).toBeVisible();
  await expect(page.getByRole('button', { name: /生成答复.*失败/ })).toBeVisible();
  await expect(page.locator('.insight-inline-metrics').first()).toContainText('未知');
  await page
    .getByRole('button', { name: /查看实际输入/ })
    .first()
    .click();
  await expect(page.locator('.insight-raw')).toContainText('fixture');
  const historyBox = await page.locator('.insight-history').boundingBox();
  const pane = page.locator('.insight-trace-pane');
  await pane.hover();
  await page.mouse.wheel(0, 600);
  await expect.poll(() => pane.evaluate((e) => e.scrollTop)).toBeGreaterThan(0);
  expect(await page.locator('.insight-history').boundingBox()).toEqual(historyBox);
  expect(await page.locator('.runs-page-scroll').evaluate((e) => e.scrollTop)).toBe(0);
  await pane.evaluate((e) => e.scrollTo(0, 0));
  await page.getByRole('button', { name: /聊天融合与支持度/ }).click();
  await page.getByText('聊天检索诊断', { exact: true }).click();
  await expect(page.getByText('已索引 8 / 10')).toBeVisible();
  await page.screenshot({
    path: info.outputPath('desktop.png'),
    fullPage: true,
    animations: 'disabled',
  });
  await page.reload();
  await page.getByRole('button', { name: '运行洞察', exact: true }).click();
  await expect(page.getByRole('region', { name: '请求执行轨迹' })).toBeVisible();
  for (const width of [1440, 1024, 768, 390, 360]) {
    await page.setViewportSize({ width, height: 900 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(
      true
    );
    await expect(page.getByRole('button', { name: '返回历史记录' })).toBeVisible();
  }
  await page.screenshot({
    path: info.outputPath('mobile.png'),
    fullPage: true,
    animations: 'disabled',
  });
  await page.getByRole('button', { name: '返回历史记录' }).click();
  await expect(page.getByRole('button', { name: /查询甲企业月度资料（观测夹具）/ })).toBeVisible();
  await page.getByRole('button', { name: '策略对比', exact: true }).click();
  await expect(page.getByText('暂无已导入报告，维护者可使用本地导入命令添加。')).toBeVisible();
  expect(errors).toEqual([]);
});
