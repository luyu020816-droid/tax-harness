import { expect, test } from '@playwright/test';

test('history quote preserves role, continues exact text, opens original session and hides revoked text', async ({
  page,
}, info) => {
  let revoked = false;
  const source = '11111111-1111-4111-8111-111111111111';
  const neighbor = '22222222-2222-4222-8222-222222222222';
  const session = {
    session_id: 'original',
    selected_company_id: 'C001',
    title: '原方案会话',
    organization_revision: 1,
  };
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname.replace('/api/v1', '');
    const reply = (json: unknown, status = 200) => route.fulfill({ json, status });
    if (path === '/me')
      return reply({
        user_id: 'U001',
        display_name: '测试用户',
        model_ready: true,
        model_mode: 'test',
        companies: [{ company_id: 'C001', name: '甲企业', can_read: true, can_update: true }],
      });
    if (path === '/sessions') return reply([session]);
    if (path === '/tasks') return reply([]);
    if (path.endsWith('/context-usage'))
      return reply({ status: 'no_calls', input_tokens: 0, effective_window: 100000 });
    if (path === '/conversations/sessions/original') return reply(session);
    if (path.endsWith('/messages'))
      return reply({
        turns: [
          {
            turn_id: 'old',
            text: '找回昨天方案',
            status: 'completed',
            final_message: '历史助手建议，尚未执行。',
            final_cards: [
              {
                kind: 'history_quote',
                source_id: source,
                citation_id: 'cite',
                session_id: 'original',
                company_id: 'C001',
                quote_start: 0,
                quote_end: 3,
              },
            ],
            runs: [],
          },
        ],
        has_more: false,
      });
    if (path === '/evidence/neighborcite')
      return revoked
        ? reply({ error: { message: '来源已失效' } }, 404)
        : reply({
            source_id: neighbor,
            session_id: 'original',
            role: 'user',
            created_at: '2026-10-03T00:00:00Z',
            excerpt: '最早用户方案',
            locator: { char_start: 0, char_end: 6 },
            next_char_start: null,
          });
    if (path === '/evidence/cite')
      return revoked
        ? reply({ error: { message: '来源已失效' } }, 404)
        : reply({
            source_id: source,
            session_id: 'original',
            role: 'assistant',
            created_at: '2026-10-03T00:00:00Z',
            neighbors: [
              {
                source_id: neighbor,
                direction: 'previous',
                role: 'user',
                created_at: '2026-10-03T00:00:00Z',
              },
            ],
            excerpt: '先备份',
            locator: { char_start: 0, char_end: 3 },
            next_char_start: 3,
          });
    if (path === '/conversations/read') {
      if (route.request().postDataJSON().source_id === neighbor)
        return reply({
          citation_id: 'neighborcite',
          source_id: neighbor,
          session_id: 'original',
          role: 'user',
          created_at: '2026-10-03T00:00:00Z',
          excerpt: '最早用户方案',
          locator: { char_start: 0, char_end: 6 },
          next_char_start: null,
        });
      expect(route.request().postDataJSON()).toEqual({ source_id: source, char_start: 3 });
      return reply({ excerpt: '再迁移，保留回退副本。', next_char_start: null });
    }
    throw new Error(`Unmocked ${path}`);
  });
  await page.goto('/showcase.html');
  await expect(page.getByText('先备份', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '继续读取这条原文', exact: true }).click();
  await expect(page.getByText('再迁移，保留回退副本。', { exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: '打开原会话' }).first()).toHaveAttribute(
    'href',
    '/showcase.html?session=original'
  );
  // Let the scheduled revalidation run; completion must not re-enable continuation.
  await expect(page.getByRole('button', { name: '继续读取这条原文', exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: '查看上一条用户原文', exact: true }).click();
  await expect(page.getByText('最早用户方案', { exact: true })).toBeVisible();
  await page.screenshot({ path: info.outputPath('history-card-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('button', { name: '打开导航', exact: true }).click();
  await page.getByRole('button', { name: '关闭导航', exact: true }).last().click();
  await expect(page.getByRole('button', { name: '打开导航', exact: true })).toBeVisible();
  await expect(page.getByText('再迁移，保留回退副本。', { exact: true })).toBeVisible();
  await page.screenshot({
    path: info.outputPath('history-card-mobile.png'),
    fullPage: true,
    animations: 'disabled',
  });
  revoked = true;
  await expect(page.getByText('先备份', { exact: true })).toHaveCount(0, { timeout: 10000 });
  await expect(page.getByText('最早用户方案', { exact: true })).toHaveCount(0);
  await expect(page.getByText('再迁移，保留回退副本。', { exact: true })).toHaveCount(0);
});
