import { expect, test } from '@playwright/test';

for (const kind of ['agent', 'skill'] as const) {
  test(`${kind}: toggling a refreshed row must not rebase an old editor`, async ({ page }) => {
    const agentId = '11111111-1111-4111-8111-111111111111';
    const skillId = '22222222-2222-4222-8222-222222222222';
    const initialDraft = {
      name: '并发助手',
      description: '原内容',
      prompt: '',
      model_profile_id: 'default',
      tool_grants: {},
      skill_ids: [],
      knowledge_mode: 'none',
      knowledge_document_ids: [],
    };
    let agent = {
      agent_id: agentId,
      name: initialDraft.name,
      revision: 1,
      current_version: 1,
      enabled: true,
      draft: initialDraft,
    };
    let skill = {
      skill_id: skillId,
      name: '并发技能',
      revision: 1,
      current_version: 1,
      enabled: true,
      config: {
        name: '并发技能',
        description: '',
        instructions: '原内容',
        tool_grants: {},
        requires_knowledge: false,
      },
    };
    let enabled = true;
    let attemptedRevision: number | undefined;
    const requests: number[] = [];
    await page.route('**/api/v1/**', async (route) => {
      const path = new URL(route.request().url()).pathname.replace('/api/v1', '');
      const method = route.request().method();
      const reply = (json: unknown, status = 200) => route.fulfill({ json, status });
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
      if (path === '/sessions')
        return reply([{ session_id: 's1', selected_company_id: 'C001', title: '新对话' }]);
      if (path.endsWith('/messages')) return reply({ turns: [], has_more: false });
      if (path === '/tasks' || path === '/agents' || path === '/agent-admin/models')
        return reply([]);
      if (path.endsWith('/context-usage'))
        return reply({ status: 'no_calls', input_tokens: 0, effective_window: 100000 });
      if (path === '/knowledge/documents') return reply({ documents: [], next_offset: null });
      if (path === '/agent-admin/agents') return reply({ agents: [agent], runtime_ready: true });
      if (path === '/agent-admin/skills') return reply([skill]);
      if (path === '/agent-admin/tools')
        return reply([
          {
            name: 'find_invoices',
            description: '测试工具',
            actions: ['invoke'],
            enabled,
            revision: 0,
          },
        ]);
      if (path === '/agent-admin/tools/find_invoices/enabled') {
        enabled = route.request().postDataJSON().enabled;
        return reply({ affected_agents: [] });
      }
      const entityPath =
        kind === 'agent' ? `/agent-admin/agents/${agentId}` : `/agent-admin/skills/${skillId}`;
      const current = kind === 'agent' ? agent : skill;
      if (path === `${entityPath}/enabled`) {
        const input = route.request().postDataJSON();
        if (input.expected_revision !== current.revision)
          return reply({ message: '版本冲突' }, 409);
        if (kind === 'agent') {
          agent = { ...agent, revision: agent.revision + 1, enabled: input.enabled };
          return reply(agent);
        }
        skill = { ...skill, revision: skill.revision + 1, enabled: input.enabled };
        const { config, ...metadata } = skill;
        return reply(metadata);
      }
      if (path === entityPath && method === 'PUT') {
        const input = route.request().postDataJSON();
        attemptedRevision = input.expected_revision;
        requests.push(input.expected_revision);
        if (input.expected_revision !== current.revision)
          return reply({ message: '版本冲突，请重新编辑' }, 409);
        if (kind === 'agent') {
          agent = { ...agent, revision: agent.revision + 1, draft: input.draft };
          return reply(agent);
        }
        skill = {
          ...skill,
          revision: skill.revision + 1,
          current_version: skill.current_version + 1,
          config: input.draft,
        };
        return reply(skill);
      }
      return reply({ message: 'not found' }, 404);
    });
    await page.goto('/showcase.html');
    await page.getByRole('button', { name: 'Agent 管理', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: 'Agent 与能力管理' });
    if (kind === 'skill') await dialog.getByText('Skill 配置', { exact: true }).click();
    const row = dialog
      .locator('p')
      .filter({ hasText: kind === 'agent' ? '并发助手 ·' : '并发技能 ·' });
    await row.getByRole('button', { name: '编辑', exact: true }).click();
    const field = dialog.getByRole('textbox', {
      name: kind === 'agent' ? '职责说明' : '任务指引',
      exact: true,
    });
    await field.fill('本地未保存内容');
    // Another manager updates the server. An unrelated operation refreshes only the lists.
    if (kind === 'agent')
      agent = {
        ...agent,
        revision: 2,
        draft: { ...agent.draft, description: '另一管理员的新修改' },
      };
    else
      skill = {
        ...skill,
        revision: 2,
        config: { ...skill.config, instructions: '另一管理员的新修改' },
      };
    await dialog.getByText('工具启停', { exact: true }).click();
    await dialog
      .locator('p')
      .filter({ hasText: 'find_invoices ·' })
      .getByRole('button', { name: '停用', exact: true })
      .click();
    await expect(dialog.getByRole('status')).toContainText('工具状态已更新');
    await row.getByRole('button', { name: '停用', exact: true }).click();
    await expect(dialog.getByRole('status')).toContainText('状态已更新');
    await expect(field).toHaveValue('本地未保存内容');
    const save = dialog.getByRole('button', {
      name: kind === 'agent' ? '保存草稿' : '发布 Skill',
      exact: true,
    });
    await save.click();
    await expect.poll(() => attemptedRevision).toBeDefined();
    expect(attemptedRevision).toBe(1);
    await expect(dialog.getByRole('alert')).toContainText('版本冲突');
    expect(kind === 'agent' ? agent.draft.description : skill.config.instructions).toBe(
      '另一管理员的新修改'
    );
    // Explicit reload restores a valid baseline; normal saving remains available.
    await row.getByRole('button', { name: '编辑', exact: true }).click();
    await expect(field).toHaveValue('另一管理员的新修改');
    await field.fill('核对后合并的修改');
    await save.click();
    await expect(dialog.getByRole('status')).toContainText(
      kind === 'agent' ? '草稿已保存' : 'Skill 版本已发布'
    );
    expect(requests).toEqual([1, 3]);
    expect(kind === 'agent' ? agent.draft.description : skill.config.instructions).toBe(
      '核对后合并的修改'
    );
  });
}
