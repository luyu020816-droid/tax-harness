import { expect, test } from '@playwright/test';

test('manual knowledge and conversation management', async ({ page }, info) => {
  const errors: string[] = [];
  page.on('pageerror', (e) => errors.push(e.message));
  const sessions = Array.from({ length: 10 }, (_, i) => ({
    session_id: `s${i}`,
    selected_company_id: 'C001',
    title: `测试会话${i}`,
    updated_at: '2026-11-02T00:00:00Z',
    organization_revision: 1,
    pinned_at: null as string | null,
    archived_at: null as string | null,
    deleted_at: null as string | null,
  }));
  const submissions: Record<string, unknown>[] = [];
  let authenticated = true;
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname.replace('/api/v1', '');
    const reply = (json: unknown, status = 200) => route.fulfill({ json, status });
    if (path === '/auth/logout') {
      authenticated = false;
      return route.fulfill({ status: 204, body: '' });
    }
    if (path === '/me' && !authenticated) return reply({}, 401);
    if (path === '/me')
      return reply({
        user_id: 'U001',
        display_name: '测试用户一',
        companies: [
          {
            company_id: 'C001',
            name: '甲企业',
            can_read: true,
            can_update: true,
            can_upload_knowledge: true,
          },
        ],
        model_ready: true,
        model_mode: 'test',
        dataset_profile: 'competition',
      });
    if (path === '/sessions')
      return reply(
        sessions
          .filter((s) => !s.archived_at && !s.deleted_at)
          .sort((a, b) => Number(!!b.pinned_at) - Number(!!a.pinned_at))
      );
    if (path === '/tasks') return reply([]);
    if (path.endsWith('/messages')) return reply({ turns: [], has_more: false });
    if (path === '/sessions/history')
      return reply({
        sessions: sessions.filter(
          (s) => !s.deleted_at && !!s.archived_at === (url.searchParams.get('archived') === 'true')
        ),
        next_offset: null,
      });
    if (path.endsWith('/organization')) {
      const s = sessions.find((s) => s.session_id === path.split('/')[2])!;
      const { action } = route.request().postDataJSON();
      s.organization_revision++;
      if (action === 'pin') s.pinned_at = '2026-11-02T00:01:00Z';
      if (action === 'unpin') s.pinned_at = null;
      if (action === 'archive') s.archived_at = '2026-11-02T00:01:00Z';
      if (action === 'unarchive') s.archived_at = null;
      if (action === 'delete') s.deleted_at = '2026-11-02T00:01:00Z';
      return reply(s);
    }
    if (path === '/knowledge/documents')
      return reply({ documents: [], has_more: false, next_offset: null });
    if (path === '/rules/catalog') return reply({ company_id: 'C001', rules: [] });
    if (path === '/knowledge/text') {
      submissions.push(route.request().postDataJSON());
      return reply({ status: 'queued', document_id: 'doc-1' }, 202);
    }
    if (path.endsWith('/context-usage'))
      return reply({ status: 'no_calls', input_tokens: 0, effective_window: 100000 });
    throw new Error(`Unmocked ${path}`);
  });
  await page.goto('/showcase.html');
  await page.getByRole('button', { name: '全部会话与归档', exact: true }).click();
  await expect(
    page
      .getByRole('dialog', { name: '历史会话' })
      .getByRole('button', { name: '打开会话：测试会话9', exact: true })
  ).toBeVisible();
  const historyDialog = page.getByRole('dialog', { name: '历史会话' });
  await historyDialog.getByLabel('会话操作：测试会话9', { exact: true }).click();
  await page.locator('.session-actions-menu').getByRole('button', { name: '置顶', exact: true }).click();
  await expect(
    page
      .locator('.recent-section')
      .getByRole('button', { name: '打开会话：测试会话9', exact: true })
  ).toBeVisible();
  await historyDialog.getByLabel('会话操作：测试会话8', { exact: true }).click();
  await page.locator('.session-actions-menu').getByRole('button', { name: '归档', exact: true }).click();
  await historyDialog.getByRole('button', { name: '已归档', exact: true }).click();
  await expect(
    historyDialog.getByRole('button', { name: '打开会话：测试会话8', exact: true })
  ).toBeVisible();
  await historyDialog.getByLabel('会话操作：测试会话8', { exact: true }).click();
  await page.locator('.session-actions-menu').getByRole('button', { name: '取消归档', exact: true }).click();
  await expect(historyDialog.getByText('暂无会话', { exact: true })).toBeVisible();
  await historyDialog.getByRole('button', { name: '全部会话', exact: true }).click();
  await historyDialog.getByLabel('会话操作：测试会话7', { exact: true }).click();
  await page.locator('.session-actions-menu').getByRole('button', { name: '删除', exact: true }).click();
  await expect(page.getByRole('dialog', { name: '删除会话' })).toContainText('知识资料');
  await page.getByRole('button', { name: '确认删除', exact: true }).click();
  await expect(
    historyDialog.getByRole('button', { name: '打开会话：测试会话7', exact: true })
  ).toHaveCount(0);
  await historyDialog.getByRole('button', { name: '关闭', exact: true }).click();
  await page.getByRole('button', { name: '知识资料', exact: true }).click();
  await page.getByRole('button', { name: '添加文字', exact: true }).click();
  expect(submissions.length).toBe(0);
  await page.getByLabel('资料标题', { exact: true }).fill('手动资料');
  await page.getByLabel('资料正文', { exact: true }).fill('必须手动提交才进入知识库。');
  await page.getByRole('button', { name: '提交到知识库', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  expect(submissions.length).toBe(1);
  expect(submissions[0].content).toBe('必须手动提交才进入知识库。');
  for (const width of [1440, 1024, 768, 390, 360]) {
    await page.setViewportSize({ width, height: 900 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(
      true
    );
    if (width === 768)
      await expect(page.getByRole('button', { name: '管理历史会话', exact: true })).toBeVisible();
  }
  await page.screenshot({ path: info.outputPath('manual-knowledge-mobile.png'), fullPage: true });
  await page.getByRole('button', { name: '退出登录', exact: true }).click();
  await expect(page.getByRole('button', { name: /测试用户一/ })).toBeVisible();
  expect(errors).toEqual([]);
});
