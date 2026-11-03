import { expect,test } from '@playwright/test';

test('live insights: existing history and imported comparison remain read-only',async({page},info)=>{
  test.skip(process.env.INSIGHTS_LIVE!=='1','Requires explicitly selected local real backend; no model calls');
  const errors:string[]=[];
  page.on('pageerror',e=>errors.push(e.message));
  const login=await page.request.post('/api/v1/demo/login',{data:{user_id:'U001'}});
  expect(login.ok()).toBe(true);
  await page.goto('/showcase.html');
  await page.getByRole('button',{name:'运行洞察',exact:true}).click();
  await expect(page.getByRole('heading',{name:'运行洞察',exact:true})).toBeVisible();
  await expect(page.locator('.insight-history-row').first()).toBeVisible();
  await page.locator('.insight-history-row').first().click();
  await expect(page.getByRole('region',{name:'请求执行轨迹'})).toBeVisible();
  await expect(page.locator('.insight-run').first()).toBeVisible();
  await page.locator('.page-scroll').evaluate(el=>{el.scrollTop=0;});
  await page.screenshot({path:info.outputPath('live-desktop.png'),fullPage:true,animations:'disabled'});
  await page.getByRole('button',{name:'策略对比',exact:true}).click();
  await page.locator('.insight-evaluation-list button').first().click();
  await expect(page.locator('.insight-table-scroll')).toContainText('summary_search');
  await expect(page.locator('.insight-table-scroll')).toContainText('18 / 18');
  expect(errors).toEqual([]);
});
