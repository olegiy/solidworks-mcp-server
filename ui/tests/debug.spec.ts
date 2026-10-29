import { test, expect } from '@playwright/test';

test('режим отладки сохраняется, пишет запрос и позволяет скачать журнал после закрытия проекта', async ({ page }) => {
  const profile = await (await page.request.post('/api/profiles', { data: {
    name: 'Отладка', base_url: 'http://localhost/v1', model: 'fake-model',
  } })).json();
  await page.addInitScript(id => {
    localStorage.setItem('cadnext.v1.profile', id);
    localStorage.setItem('cadnext.v1.goalOptions', JSON.stringify({ enabled: false,
      target_similarity: 100, max_evaluations: 10, max_no_progress: 3 }));
  }, profile.id);
  await page.goto('/');
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  const toggle = page.getByLabel('Режим отладки', { exact: true });
  await toggle.check();
  await expect(toggle).toBeEnabled();
  await expect(page.getByText('Режим отладки включён.', { exact: true })).toBeVisible();
  await page.reload();
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  await expect(toggle).toBeChecked();
  await expect(page.getByTestId('debug-log-path')).toContainText('debug.jsonl');
  await page.getByRole('button', { name: 'Закрыть настройки', exact: true }).click();
  await page.getByLabel('Сообщение', { exact: true }).fill('Прочитай размер');
  await page.getByRole('button', { name: 'Отправить', exact: true }).click();
  await expect(page.getByTestId('run-status')).toHaveText('Завершён');
  await page.getByRole('button', { name: 'Закрыть проект', exact: true }).click();
  await expect(page.getByTestId('run-status')).toHaveCount(0);
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  const pending = page.waitForEvent('download');
  await page.getByRole('link', { name: 'Скачать журнал отладки', exact: true }).click();
  const download = await pending;
  expect(download.suggestedFilename()).toBe('cadnext-debug.zip');
  expect(await download.failure()).toBeNull();
  await toggle.uncheck();
  await expect(page.getByText('Режим отладки выключен.', { exact: true })).toBeVisible();
  expect((await (await page.request.get('/api/settings')).json()).debug_logging_enabled).toBe(false);
});

test('ошибка сохранения режима отладки сохраняет прежнее положение переключателя', async ({ page }) => {
  await page.request.put('/api/settings', { data: { debug_logging_enabled: false } });
  await page.goto('/');
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  await page.route('**/api/settings', route => route.request().method() === 'PUT'
    ? route.fulfill({ status: 500, json: { detail: 'Не удалось записать настройки отладки.' } })
    : route.continue());
  const toggle = page.getByLabel('Режим отладки', { exact: true });
  await toggle.click();
  await expect(page.getByRole('alert')).toContainText('Не удалось записать настройки отладки.');
  await expect(toggle).not.toBeChecked();
  await expect(toggle).toBeEnabled();
});
