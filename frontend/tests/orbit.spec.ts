import { expect, test } from '@playwright/test';

test('orbit chat: draft, retry identity, polling, history, sessions, and mobile', async ({
  page,
}) => {
  let authenticated = false;
  let failedOnce = false;
  const requests: { client_request_id: string; text: string }[] = [];
  const history: Record<string, object[]> = { s1: [] };
  const sessions = [
    {
      session_id: 's1',
      selected_company_id: 'C001',
      title: '界面协议测试',
      updated_at: '2026-11-01T00:00:00Z',
    },
  ];
  let polls = 0;
  const turn = {
    turn_id: 't1',
    source_seq: 1,
    text: '',
    status: 'running',
    final_message: null as string | null,
    final_cards: [],
    next_event_seq: 0,
    active_run_generation: 1,
    error: null,
  };
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname.replace('/api/v1', '');
    const method = route.request().method();
    const reply = (json: unknown, status = 200) => route.fulfill({ status, json });
    if (path === '/me')
      return authenticated
        ? reply({
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
          })
        : reply({ message: '请登录' }, 401);
    if (path === '/demo/login') {
      authenticated = true;
      return reply({});
    }
    if (path === '/sessions' && method === 'GET') return reply(sessions);
    if (path === '/sessions' && method === 'POST') {
      const session = { ...sessions[0], session_id: 's2', title: '新对话' };
      sessions.unshift(session);
      history.s2 = [];
      return reply(session);
    }
    if (path.endsWith('/messages'))
      return reply({ turns: history[path.split('/')[2]] ?? [], has_more: false });
    if (path.endsWith('/context-usage'))
      return reply({ status: 'no_calls', input_tokens: 0, effective_window: 100000 });
    if (path === '/tasks') return reply([]);
    if (path.endsWith('/events'))
      return route.fulfill({
        contentType: 'text/event-stream',
        body: 'event: model_started\ndata: {}\n\n',
      });
    if (path === '/sessions/s1/turns') {
      const request = route.request().postDataJSON();
      requests.push(request);
      if (!failedOnce) {
        failedOnce = true;
        return route.fulfill({ status: 503, body: 'Service unavailable' });
      }
      turn.text = request.text;
      history.s1 = [turn];
      return reply(turn);
    }
    if (path === '/turns/t1') {
      polls++;
      if (polls > 1) {
        turn.status = 'completed';
        turn.final_message = '已完成界面协议测试。此答复来自测试夹具，未调用真实模型。';
      }
      return reply(turn);
    }
    throw new Error(`Unmocked endpoint: ${method} ${path}`);
  });
  await page.goto('/showcase.html');
  await page.getByRole('button', { name: /测试用户一/ }).click();
  const input = page.getByRole('textbox', { name: '输入办理需求' });
  await expect(input).toBeVisible();
  await expect(page.getByRole('heading', { name: /让复杂财税/ })).toBeVisible();
  await page.getByRole('button', { name: /查询月度资料/ }).click();
  await expect(input).toHaveValue(/2026年9月/);
  await expect(page.locator('.turn')).toHaveCount(0);
  await page.screenshot({ path: 'orbit-chat-desktop.png', fullPage: true, animations: 'disabled' });
  await input.fill('中文输入法候选');
  await input.dispatchEvent('compositionstart');
  await input.press('Enter');
  expect(requests).toHaveLength(0);
  await input.dispatchEvent('compositionend');
  await input.fill('查询甲企业2026年9月月度资料');
  await page.getByRole('button', { name: '发送消息' }).click();
  await expect(page.getByRole('alert')).toContainText('业务服务返回异常');
  await expect(input).toHaveValue('查询甲企业2026年9月月度资料');
  await page.getByRole('button', { name: '发送消息' }).click();
  await expect(page.getByRole('button', { name: '停止本轮处理' })).toBeVisible();
  await expect(page.locator('.prose')).toContainText('已完成界面协议测试');
  expect(requests).toHaveLength(2);
  expect(requests[0].client_request_id).toBe(requests[1].client_request_id);
  await page.reload();
  await expect(page.locator('.prose')).toContainText('已完成界面协议测试');
  await page.screenshot({ path: 'orbit-chat-result.png', fullPage: true, animations: 'disabled' });
  for (const width of [1440, 1024, 768, 390, 360]) {
    await page.setViewportSize({ width, height: 844 });
    await expect(input).toBeInViewport();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(
      true
    );
  }
  await page.getByRole('button', { name: '打开导航' }).click();
  await page.getByRole('button', { name: '新对话', exact: true }).click();
  await expect(page.locator('.turn')).toHaveCount(0);
  await expect(input).toBeInViewport();
  await page.screenshot({ path: 'orbit-chat-mobile.png', fullPage: true, animations: 'disabled' });
  expect(errors).toEqual([]);
});
