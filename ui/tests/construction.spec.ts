import { expect, test } from '@playwright/test';

test('этапы из настоящего API сохраняются после обновления, код и доказательства раскрываются', async ({ page }) => {
  const profile = await (await page.request.post('/api/profiles', { data: {
    name: 'Этапы', base_url: 'http://localhost/v1', model: 'fake-model',
  } })).json();
  await page.request.put('/api/settings', { data: { python_execution_allowed: true } });
  await page.addInitScript(id => {
    localStorage.setItem('cadnext.v1.profile', id);
    localStorage.setItem('cadnext.v1.session', 'construction-demo');
  }, profile.id);
  await page.goto('/');
  await page.locator('.goal-inline summary').click();
  await page.getByLabel('Проверять цель до достижения').uncheck();
  await page.locator('.goal-inline summary').click();
  await page.getByRole('textbox', { name: 'Сообщение', exact: true }).fill('stage-demo');
  await page.getByRole('button', { name: 'Отправить', exact: true }).click();
  const plan = page.getByRole('region', { name: 'Этапы построения' });
  await expect(plan.getByText('Профиль втулки', { exact: true })).toBeVisible();
  await expect(plan.getByText('Завершён', { exact: true })).toHaveCount(2);
  await expect(plan.getByText('Проверка пройдена', { exact: true })).toBeVisible();
  await expect(plan.getByText('result = 80', { exact: true })).not.toBeVisible();
  await plan.getByText('Код этапа', { exact: true }).first().click();
  await expect(plan.getByText('result = 80', { exact: true })).toBeVisible();
  await plan.getByText('Доказательства', { exact: true }).click();
  await expect(plan.getByText('Length@Profile', { exact: false }).first()).toBeVisible();
  await expect(page.getByTestId('run-status')).toHaveText('Завершён');
  await page.reload();
  await expect(plan.getByText('Профиль втулки', { exact: true })).toBeVisible();
  await expect(plan.getByText('Проверка пройдена', { exact: true })).toBeVisible();
});
