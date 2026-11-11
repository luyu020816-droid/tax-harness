import { expect, test } from '@playwright/test';

test('switching companies reuses an empty session and preserves a conversation with messages', async ({ page }) => {
  let authenticated = false;
  const sessions = [{ session_id: 's1', selected_company_id: 'C001', title: '新对话', updated_at: '2026-11-01' }];
  const history: Record<string, object[]> = { s1: [] };
  let creates = 0;
  let switches = 0;
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname.replace('/api/v1', '');
    const method = route.request().method();
    const reply = (json: unknown, status = 200) => route.fulfill({ status, json });
    if (path === '/me') return authenticated ? reply({ user_id: 'U001', display_name: '一',
      companies: [{ company_id: 'C001', name: '甲', can_update: true }, { company_id: 'C002', name: '乙', can_update: true }],
      model_ready: true, model_mode: 'test', dataset_profile: 'competition', invoice_import_ready: false }) : reply({}, 401);
    if (path === '/demo/login') { authenticated = true; return reply({}); }
    if (path === '/sessions' && method === 'GET') return reply(sessions);
    if (path === '/sessions' && method === 'POST') {
      creates++;
      const item = { ...sessions[0], session_id: `s${creates + 1}`, selected_company_id: route.request().postDataJSON().selected_company_id };
      sessions.unshift(item);
      history[item.session_id] = [];
      return reply(item, 201);
    }
    if (path.endsWith('/empty-company')) {
      const item = sessions.find((session) => session.session_id === path.split('/')[2])!;
      if (history[item.session_id].length) return reply({ code: 'SESSION_NOT_EMPTY', message: '已有消息' }, 409);
      switches++;
      item.selected_company_id = route.request().postDataJSON().selected_company_id;
      return reply(item);
    }
    if (path.endsWith('/messages')) return reply({ turns: history[path.split('/')[2]] ?? [], has_more: false });
    if (path === '/tasks') return reply([]);
    if (path.endsWith('/context-preview')) return reply({ input_estimate: 10000, input_limit: 92952,
      output_reserve: 2048, margin: 5000, effective_window: 100000, pressure_ratio: 0.17048,
      window_status: 'configured', count_kind: 'estimated:utf8_div2_plus_protocol' });
    if (path === '/sessions/s1/turns') {
      const turn = { turn_id: 't1', source_seq: 1, text: '你好', status: 'completed', final_message: '你好', final_cards: [], next_event_seq: 0, active_run_generation: 1, error: null };
      history.s1 = [turn];
      return reply(turn, 202);
    }
    if (path === '/turns/t1') return reply(history.s1[0]);
    if (path.endsWith('/events')) return route.fulfill({ contentType: 'text/event-stream', body: '' });
    return reply({ message: 'not found' }, 404);
  });
  await page.goto('/showcase.html');
  await page.getByRole('button', { name: /测试用户一/ }).click();
  const company = page.getByLabel('当前企业');
  for (const value of ['C002', 'C001', 'C002']) {
    await company.selectOption(value);
    await expect(company).toHaveValue(value);
    await expect(company).toBeEnabled();
  }
  expect(switches).toBe(3);
  expect(creates).toBe(0);
  await page.getByRole('textbox', { name: '输入办理需求' }).fill('你好');
  await page.getByRole('button', { name: '发送消息' }).click();
  await expect(page.locator('.prose')).toContainText('你好');
  await company.selectOption('C001');
  await expect(company).toHaveValue('C001');
  await expect(company).toBeEnabled();
  expect(creates).toBe(1);
  expect(sessions.find((s) => s.session_id === 's1')?.selected_company_id).toBe('C002');
  expect(history.s1).toHaveLength(1);
  await company.selectOption('C002');
  await expect(company).toHaveValue('C002');
  await expect(company).toBeEnabled();
  expect(creates).toBe(1);
  expect(switches).toBe(4);
});
