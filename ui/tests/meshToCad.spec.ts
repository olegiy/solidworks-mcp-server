import { expect, test } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { resolve } from 'node:path';

test('сетка → этапы → независимые размеры → две стороны сравнения', async ({ page }) => {
  const root = resolve(process.cwd(), '..');
  const buffer = execFileSync(resolve(root, '.venv/Scripts/python.exe'), ['-c',
    'import sys; from tests.mesh_fixtures import make_plate_with_hole, encode_fixture; sys.stdout.buffer.write(encode_fixture(make_plate_with_hole(), "obj"))'],
    { cwd: root });
  const profile = await (await page.request.post('/api/profiles', { data: {
    name: 'Mesh fixture', base_url: 'http://localhost/v1', model: 'mesh-fixture', vision: false,
  } })).json();
  await page.request.put('/api/settings', { data: { python_execution_allowed: true } });
  await page.addInitScript(id => {
    localStorage.setItem('cadnext.v1.profile', id);
    if (!sessionStorage.getItem('cadnext.v1.session')) sessionStorage.setItem('cadnext.v1.session', crypto.randomUUID());
    localStorage.setItem('cadnext.v1.goalOptions', JSON.stringify({ enabled: false,
      target_similarity: 100, max_evaluations: 10, max_no_progress: 3 }));
  }, profile.id);
  await page.goto('/');
  await page.getByText('Эталон STL/OBJ', { exact: true }).click();
  await page.getByLabel('Единицы сетки').selectOption('mm');
  await page.getByLabel('Файл STL или OBJ').setInputFiles({ name: 'plate.obj', mimeType: 'text/plain', buffer });
  await expect(page.getByTestId('mesh-state')).toContainText('Готов');
  await page.getByLabel('Сообщение', { exact: true }).fill('mesh-build:plate');
  const response = page.waitForResponse(r => r.url().endsWith('/api/runs') && r.request().method() === 'POST');
  await page.getByRole('button', { name: 'Отправить', exact: true }).click();
  const runId = (await (await response).json()).id;
  await expect(page.getByText('Этапы и размеры проверены. Точность CAD-экспорта пока не подтверждена.',
    { exact: true })).toBeVisible({ timeout: 30000 });
  const report = page.getByRole('region', { name: 'Соответствие сетке' });
  await expect(report).toContainText('Не подтверждено');
  await expect(report).toContainText('Исходник → CAD');
  await expect(report).toContainText('CAD → исходник');
  const run = await (await page.request.get(`/api/runs/${runId}`)).json();
  expect(run.construction_plan.map((s: { status: string }) => s.status)).toEqual(['done', 'done']);
  expect(run.call_count).toBeLessThan(80);
  expect(run.events.filter((e: { type: string; data: { name: string } }) =>
    e.type === 'tool_start' && e.data.name === 'execute_python')).toHaveLength(2);
  await page.reload();
  await expect(page.getByRole('region', { name: 'Соответствие сетке' })).toContainText('Не подтверждено');
});
