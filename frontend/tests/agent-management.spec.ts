import { expect, test } from '@playwright/test';

test('manager configures an agent and chooses it for a conversation', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  let agent: any = null;
  let submitted: any = null;
  let skill: any = {
    skill_id: '22222222-2222-4222-8222-222222222222',
    name: '核对 Skill',
    revision: 1,
    current_version: 1,
    enabled: true,
    config: {
      name: '核对 Skill',
      description: '',
      instructions: '先核对票号',
      tool_grants: {},
      requires_knowledge: false,
    },
  };
  const agentId = '11111111-1111-4111-8111-111111111111';
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname.replace('/api/v1', '');
    const method = route.request().method();
    const reply = (json: unknown, status = 200) => route.fulfill({ status, json });
    if (path === '/me')
      return reply({
        user_id: 'U001',
        display_name: '一',
        companies: [{ company_id: 'C001', name: '甲', can_update: true }],
        model_ready: true,
        model_mode: 'test',
        dataset_profile: 'competition',
        invoice_import_ready: false,
      });
    if (path === '/sessions' && method === 'GET')
      return reply([{ session_id: 's1', selected_company_id: 'C001', title: '新对话' }]);
    if (path.endsWith('/messages')) return reply({ turns: [], has_more: false });
    if (path === '/tasks') return reply([]);
    if (path.endsWith('/context-usage'))
      return reply({ status: 'no_calls', input_tokens: 0, effective_window: 100000 });
    if (path === '/agents')
      return reply(
        agent?.enabled
          ? [{ agent_id: agentId, name: agent.name, description: '', model_ready: true }]
          : []
      );
    if (path === '/agent-admin/agents' && method === 'GET')
      return reply({ agents: agent ? [agent] : [], runtime_ready: true });
    if (path === '/agent-admin/agents' && method === 'POST') {
      const draft = route.request().postDataJSON();
      agent = {
        agent_id: agentId,
        name: draft.name,
        draft,
        revision: 1,
        current_version: null,
        enabled: false,
      };
      return reply(agent, 201);
    }
    if (path === `/agent-admin/agents/${agentId}/publish`) {
      if (route.request().postDataJSON().expected_revision !== agent.revision)
        return reply({ message: '版本冲突' }, 409);
      agent = {
        ...agent,
        revision: agent.revision + 1,
        current_version: (agent.current_version ?? 0) + 1,
      };
      return reply(agent);
    }
    if (path === `/agent-admin/agents/${agentId}` && method === 'PUT') {
      const input = route.request().postDataJSON();
      if (input.expected_revision !== agent.revision) return reply({ message: '版本冲突' }, 409);
      agent = { ...agent, revision: agent.revision + 1, draft: input.draft };
      return reply(agent);
    }
    if (path === `/agent-admin/agents/${agentId}/enabled`) {
      const input = route.request().postDataJSON();
      if (input.expected_revision !== agent.revision) return reply({ message: '版本冲突' }, 409);
      agent = { ...agent, revision: agent.revision + 1, enabled: input.enabled };
      return reply(agent);
    }
    if (path === '/agent-admin/tools')
      return reply([
        {
          name: 'find_invoices',
          description: '查询发票',
          actions: ['invoke'],
          enabled: true,
          revision: 0,
        },
      ]);
    if (path === '/agent-admin/skills') return reply([skill]);
    if (path === `/agent-admin/skills/${skill.skill_id}/enabled`) {
      const input = route.request().postDataJSON();
      if (input.expected_revision !== skill.revision) return reply({ message: '版本冲突' }, 409);
      skill = { ...skill, revision: skill.revision + 1, enabled: input.enabled };
      const { config, ...metadata } = skill;
      return reply(metadata);
    }
    if (path === `/agent-admin/skills/${skill.skill_id}` && method === 'PUT') {
      const input = route.request().postDataJSON();
      if (input.expected_revision !== skill.revision) return reply({ message: '版本冲突' }, 409);
      skill = {
        ...skill,
        revision: skill.revision + 1,
        current_version: skill.current_version + 1,
        config: input.draft,
      };
      return reply(skill);
    }
    if (path === '/agent-admin/models') return reply([]);
    if (path === '/knowledge/documents') {
      const later = new URL(route.request().url()).searchParams.has('offset');
      return reply({
        documents: [
          {
            document_id: later ? 'doc2' : 'doc1',
            title: later ? '第二页资料' : '第一页资料',
            status: 'active',
            active_version_id: 'v1',
          },
        ],
        next_offset: later ? null : 20,
      });
    }
    if (path === '/sessions/s1/turns') {
      submitted = route.request().postDataJSON();
      return reply(
        {
          turn_id: 't1',
          source_seq: 1,
          text: submitted.text,
          status: 'completed',
          final_message: '已查询',
          final_cards: [],
          next_event_seq: 0,
          active_run_generation: 1,
          error: null,
        },
        202
      );
    }
    return reply({ message: 'not found' }, 404);
  });
  await page.goto('/showcase.html');
  await page.getByRole('button', { name: 'Agent 管理' }).click();
  const dialog = page.getByRole('dialog', { name: 'Agent 与能力管理' });
  await dialog.getByLabel('名称', { exact: true }).fill('发票助手');
  await dialog.getByLabel('职责提示词').fill('查询时先核对企业');
  await dialog.getByLabel('知识范围').selectOption('selected');
  await dialog.getByLabel('第一页资料').check();
  await dialog.getByRole('button', { name: '加载更多资料' }).click();
  await dialog.getByLabel('第二页资料').check();
  await dialog
    .getByText(/工具和动作授权/)
    .first()
    .click();
  await dialog.getByLabel('invoke').first().check();
  await dialog.getByRole('button', { name: '保存草稿', exact: true }).click();
  await expect(dialog.getByRole('status')).toContainText('草稿已保存');
  await dialog.getByLabel('职责提示词').fill('先核对企业，再查询发票');
  await expect(dialog.getByRole('button', { name: '发布已保存草稿' })).toBeDisabled();
  await expect(dialog).toContainText('有未保存修改');
  await dialog.getByRole('button', { name: '保存草稿', exact: true }).click();
  await expect(dialog.getByRole('button', { name: '发布已保存草稿' })).toBeEnabled();
  await dialog.getByRole('button', { name: '发布已保存草稿' }).click();
  await expect(dialog.getByRole('status')).toContainText('已发布');
  await dialog.getByRole('button', { name: '启用', exact: true }).first().click();
  await expect(dialog.getByRole('status')).toContainText('状态已更新');
  await dialog.getByLabel('职责说明').fill('启用后继续编辑');
  await dialog.getByRole('button', { name: '保存草稿', exact: true }).click();
  await expect(dialog.getByRole('status')).toContainText('草稿已保存');
  expect(agent.draft.description).toBe('启用后继续编辑');
  await dialog.getByText('工具启停', { exact: true }).click();
  await expect(dialog).toContainText('引用此工具的 Agent（含草稿）：发票助手（启用）');
  await dialog.getByText('Skill 配置', { exact: true }).click();
  const skillRow = dialog.locator('p').filter({ hasText: '核对 Skill · v1' });
  await skillRow.getByRole('button', { name: '编辑', exact: true }).click();
  await skillRow.getByRole('button', { name: '停用', exact: true }).click();
  await expect(dialog.getByRole('status')).toContainText('Skill 状态已更新');
  await dialog.getByLabel('任务指引').fill('停用后仍可编辑新版本');
  await dialog.getByRole('button', { name: '发布 Skill', exact: true }).click();
  await expect(dialog.getByRole('status')).toContainText('Skill 版本已发布');
  expect(skill.config.instructions).toBe('停用后仍可编辑新版本');
  await dialog.getByRole('button', { name: '关闭' }).click();
  await page.getByLabel('当前 Agent').selectOption(agentId);
  await page.getByRole('textbox', { name: '输入办理需求' }).fill('查询发票');
  await page.getByRole('button', { name: '发送消息' }).click();
  await expect.poll(() => submitted?.agent_id).toBe(agentId);
  expect(agent.draft.tool_grants).toEqual({ find_invoices: ['invoke'] });
  expect(agent.draft.knowledge_document_ids).toEqual(['doc1', 'doc2']);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});
