import { expect, test } from '@playwright/test';

for (const mode of ['network', 'invalid-success', 'invalid-error', 'missing-turn', 'denied']) {
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
      if (path === '/tasks') return reply([]);
      if (path.endsWith('/messages'))
        return reply({ has_more: false, turns: accepted ? [turn] : [] });
      if (path === '/sessions/network-session/turns' && route.request().method() === 'POST') {
        requests.push(route.request().postDataJSON());
        accepted = true; // Simulates acceptance followed by a lost response, not real business data.
        if (requests.length === 1) {
          if (mode === 'invalid-success') return route.fulfill({ status: 200, json: null });
          if (mode === 'missing-turn') return route.fulfill({ status: 200, json: {} });
          if (mode === 'invalid-error') return route.fulfill({ status: 503, json: null });
          if (mode === 'denied')
            return route.fulfill({
              status: 403,
              json: { code: 'ACCESS_DENIED', message: '只读身份不能修改' },
            });
          return route.abort('connectionfailed');
        }
        return reply(turn);
      }
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
    await expect(page.getByRole('alert')).toContainText(/暂时无法确认.*处理状态/);
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
