import { expect, test } from '@playwright/test';

test('local model settings test the env or enable a validated draft', async ({ page }) => {
  const requests: Record<string, unknown>[] = [];
  let status = {
    configured: true,
    source: 'env',
    model_id: 'env-model',
    test_status: 'untested',
  };
  await page.setViewportSize({ width: 390, height: 844 });
  await page.route('**/api/v1/me', (route) =>
    route.fulfill({ status: 401, json: { code: 'UNAUTHENTICATED', message: '请登录' } })
  );
  await page.route('**/api/v1/model-connection', (route) => route.fulfill({ json: status }));
  await page.route('**/api/v1/model-connection/test', async (route) => {
    const input = route.request().postDataJSON();
    requests.push(input);
    if (input.api_key === 'wrong-fixture-key') {
      return route.fulfill({
        status: 422,
        json: { code: 'MODEL_AUTH_FAILED', message: '认证失败，请检查 API Key。' },
      });
    }
    status = {
      ...status,
      ...(input.model_id ? { source: 'page', model_id: input.model_id } : {}),
      test_status: 'success',
    };
    return route.fulfill({ json: status });
  });

  await page.goto('/showcase.html');
  await page.getByRole('button', { name: '模型设置' }).click();
  const dialog = page.getByRole('dialog', { name: '本机模型设置' });
  await expect(dialog.getByRole('status')).toContainText('已配置，待测试');
  await dialog.getByRole('button', { name: '测试连接', exact: true }).click();
  await expect(dialog.getByRole('status')).toContainText('连接成功');
  expect(requests[0]).toEqual({});

  await dialog.getByLabel('API 地址').fill('https://api.example.test/v1');
  await dialog.getByLabel('模型名称').fill('new-model');
  const key = dialog.getByLabel('API Key');
  await expect(key).toHaveAttribute('type', 'password');
  await key.fill('wrong-fixture-key');
  await expect(dialog.getByRole('status')).toContainText('待测试');
  await dialog.getByRole('button', { name: '测试并启用' }).click();
  await expect(dialog.getByRole('status')).toContainText('连接失败');
  await expect(dialog.getByRole('status')).toContainText('env-model');
  await expect(key).toHaveValue('');
  await key.fill('valid-fixture-key');
  await dialog.getByRole('button', { name: '测试并启用' }).click();
  await expect(dialog.getByRole('status')).toContainText('连接成功');
  await expect(dialog.getByRole('status')).toContainText('new-model');
  await expect(key).toHaveValue('');
  expect(requests).toHaveLength(3);
  expect(
    await page.evaluate(() => JSON.stringify(localStorage) + JSON.stringify(sessionStorage))
  ).not.toContain('valid-fixture-key');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});
