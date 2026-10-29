import { expect, test } from '@playwright/test';

test('real SSE updates CAD state and completion without polling snapshots', async ({ page }) => {
  const profile = await (await page.request.post('/api/profiles', { data: {
    name: 'SSE performance', base_url: 'http://localhost/v1', model: 'fake-model',
  } })).json();
  await page.addInitScript(id => {
    localStorage.setItem('cadnext.v1.profile', id);
    localStorage.setItem('cadnext.v1.session', 'sse-performance');
    localStorage.setItem('cadnext.v1.goalOptions', JSON.stringify({
      enabled: false, target_similarity: 100, max_evaluations: 10, max_no_progress: 3 }));
  }, profile.id);
  const snapshots: string[] = [];
  page.on('request', request => {
    if (request.method() === 'GET' && /\/api\/runs\/[^/?]+$/.test(request.url())) snapshots.push(request.url());
  });
  await page.goto('/');
  await page.getByLabel('Сообщение', { exact: true }).fill('Читай долго');
  await page.getByRole('button', { name: 'Отправить', exact: true }).click();
  await expect(page.getByRole('region', { name: 'Ход выполнения', exact: true })
    .getByRole('status')).toHaveText('SolidWorks: чтение размера');
  await expect(page.getByTestId('run-status').last()).toHaveText('Завершён');
  expect(snapshots).toEqual([]);
});
