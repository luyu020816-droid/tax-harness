import { expect, test } from '@playwright/test';

test('natural knowledge answer identifies its evidence and insufficient answer has no unrelated citation', async ({ page }) => {
  let expired = false;
  const turns = [
    { turn_id: 't1', source_seq: 1, text: '南京住宿多少？', status: 'completed',
      final_message: '南京住宿上限为每人每晚437元。（来源1）',
      final_cards: [{ kind: 'knowledge_quote', citation_id: 'e1', reference_label: '来源1', company_id: 'C001', title: '差旅制度', edition: 1, locator_label: '第1段' }], runs: [] },
    { turn_id: 't2', source_seq: 2, text: '北京呢？', status: 'completed',
      final_message: '关于“北京呢？”，我这次没有找到足以确认答案的资料。', final_cards: [], runs: [] },
  ];
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname.replace('/api/v1', '');
    const reply = (json: unknown) => route.fulfill({ status: 200, json });
    if (path === '/me') return reply({ user_id: 'U001', display_name: '一', companies: [{ company_id: 'C001', name: '甲', can_update: true }], model_ready: true, model_mode: 'test', dataset_profile: 'competition' });
    if (path === '/sessions') return reply([{ session_id: 's1', selected_company_id: 'C001', title: '测试会话', updated_at: '2026-11-01' }]);
    if (path.endsWith('/messages')) return reply({ turns, has_more: false });
    if (path === '/tasks') return reply([]);
    if (path.endsWith('/context-usage')) return reply({ status: 'no_calls', input_tokens: 0, effective_window: 100000 });
    if (path === '/evidence/e1') return expired
      ? route.fulfill({ status: 409, json: { code: 'SOURCE_INVALIDATED', message: '资料已停用' } })
      : reply({ citation_id: 'e1', title: '差旅制度', edition: 1, locator: [{ type: 'docx_paragraph', paragraph: 1 }], excerpt: '南京住宿每人每晚437元。' });
    return reply({});
  });
  await page.goto('/showcase.html');
  const first = page.locator('.turn').nth(0);
  await expect(first.locator('.prose')).toContainText('南京住宿上限');
  await expect(first.locator('.knowledge-citation')).toContainText('来源1');
  await expect(first.locator('.knowledge-citation')).toContainText('南京住宿每人每晚437元');
  const second = page.locator('.turn').nth(1);
  await expect(second.locator('.prose')).toContainText('没有找到足以确认答案');
  await expect(second.locator('.knowledge-citation')).toHaveCount(0);
  expired = true;
  await expect(first.getByRole('status').filter({ hasText: '知识依据已变化或暂不可用' })).toBeVisible({ timeout: 12000 });
  await expect(first.locator('.prose')).toHaveCount(0);
});
