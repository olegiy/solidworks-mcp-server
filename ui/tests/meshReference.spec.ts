import { expect, test, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';

const mesh = { name: 'эталон.obj', mimeType: 'text/plain', buffer: Buffer.from(
  'v 0 0 0\nv 10 0 0\nv 0 10 0\nv 0 0 10\nf 1 3 2\nf 1 2 4\nf 1 4 3\nf 2 3 4\n') };

async function prepare(page: Page) {
  const result = await page.request.post('/api/profiles', { data: {
    name: 'Сетка', base_url: 'http://localhost/v1', model: 'fake-model', vision: false,
  } });
  const profile = await result.json();
  await page.addInitScript(id => {
    localStorage.setItem('cadnext.v1.profile', id);
    sessionStorage.setItem('cadnext.v1.session', crypto.randomUUID());
    localStorage.setItem('cadnext.v1.goalOptions', JSON.stringify({ enabled: false,
      target_similarity: 100, max_evaluations: 10, max_no_progress: 3 }));
  }, profile.id);
  await page.goto('/');
  await expect(page.getByLabel('Активное подключение')).toHaveValue(profile.id);
  await page.getByText('Эталон STL/OBJ', { exact: true }).click();
}

test('STL/OBJ доступен без Vision, проходит обработку и передаёт эталон в Run', async ({ page }) => {
  await prepare(page);
  await page.getByLabel('Единицы сетки').selectOption('mm');
  await page.getByLabel('Файл STL или OBJ').setInputFiles(mesh);
  await expect(page.getByTestId('mesh-state')).toContainText('Готов');
  await expect(page.getByTestId('mesh-bounds')).toContainText('10 × 10 × 10');
  await page.getByLabel('Допуск сетки, мм').fill('0.1');
  await page.getByLabel('Сообщение', { exact: true }).fill('mesh-inspect');
  const request = page.waitForRequest(r => r.url().endsWith('/api/runs') && r.method() === 'POST');
  await page.getByRole('button', { name: 'Отправить', exact: true }).click();
  const body = (await request).postDataJSON();
  expect(body.mesh_reference.asset_id).toBeTruthy();
  expect(body.mesh_reference.tolerance_mm).toBe(.1);
  expect(body.attachments).toEqual([]);
  await expect(page.getByText('Сетка измерена. Соответствие CAD пока не подтверждено.', { exact: true })).toBeVisible();
  await expect(page.getByRole('region', { name: 'Соответствие сетке' })).toContainText('Не подтверждено');
});

test('ошибка STL/OBJ сохраняет черновик; неверный допуск блокирует отправку', async ({ page }) => {
  await prepare(page);
  await page.getByLabel('Сообщение', { exact: true }).fill('Сохранить черновик');
  await page.getByLabel('Единицы сетки').selectOption('mm');
  await page.getByLabel('Файл STL или OBJ').setInputFiles({ ...mesh, buffer: Buffer.from('v nan 0 0') });
  await expect(page.getByTestId('mesh-state')).toContainText('Ошибка');
  await expect(page.getByLabel('Сообщение', { exact: true })).toHaveValue('Сохранить черновик');
  await page.getByLabel('Файл STL или OBJ').setInputFiles(mesh);
  await expect(page.getByTestId('mesh-state')).toContainText('Готов');
  await page.getByLabel('Допуск сетки, мм').fill('0');
  await expect(page.getByRole('button', { name: 'Отправить', exact: true })).toBeDisabled();
});

test('случайный OBJ-drop не отправляет форму и не меняет текст', async ({ page }) => {
  await prepare(page);
  await page.getByLabel('Сообщение', { exact: true }).fill('Мой черновик');
  const transfer = await page.evaluateHandle(() => {
    const data = new DataTransfer();
    data.items.add(new File(['v 0 0 0'], 'mesh.obj', { type: 'text/plain' }));
    return data;
  });
  await page.locator('main').dispatchEvent('drop', { dataTransfer: transfer });
  await expect(page.getByLabel('Сообщение', { exact: true })).toHaveValue('Мой черновик');
  await expect(page.locator('.run')).toHaveCount(0);
});

test('повторное открытие страницы продолжает ожидать обработку сетки', async ({ page }) => {
  let reads = 0;
  await page.route('**/api/projects/*/mesh', async route => {
    reads++;
    await route.fulfill({ json: reads < 3 ? { state: 'processing' }
      : { state: 'ready', asset_id: 'test-ready', summary: { bounds_mm: [[0,0,0],[10,10,10]],
        triangles: 4, watertight: true, winding_consistent: true } } });
  });
  await prepare(page);
  await expect(page.getByTestId('mesh-state')).toContainText('Готов');
});

test('отмена обработки сохраняет черновик и освобождает форму', async ({ page }) => {
  let removed = false;
  await page.route('**/api/projects/*/mesh', async route => {
    if (route.request().method() === 'DELETE') {
      removed = true; await route.fulfill({ status: 204 }); return;
    }
    await route.fulfill({ json: { state: removed ? 'empty' : 'processing' } });
  });
  await prepare(page);
  await page.getByLabel('Сообщение', { exact: true }).fill('Черновик после отмены');
  await page.getByRole('button', { name: 'Отменить обработку сетки' }).click();
  await expect(page.getByTestId('mesh-state')).toContainText('Эталон не выбран');
  await expect(page.getByLabel('Сообщение', { exact: true })).toHaveValue('Черновик после отмены');
  await expect(page.getByRole('button', { name: 'Отправить', exact: true })).toBeEnabled();
});

test('сохранённый проект переносит STL/OBJ через явный выбор исходника', async ({ page }) => {
  await prepare(page);
  await page.getByLabel('Единицы сетки').selectOption('mm');
  await page.getByLabel('Файл STL или OBJ').setInputFiles(mesh);
  await expect(page.getByTestId('mesh-state')).toContainText('Готов');
  await page.getByLabel('Сообщение', { exact: true }).fill('mesh-inspect');
  await page.getByRole('button', { name: 'Отправить', exact: true }).click();
  await expect(page.getByText('Сетка измерена. Соответствие CAD пока не подтверждено.', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Сохранить проект', exact: true }).click();
  const notice = page.getByTestId('save-notice');
  await expect(notice).toContainText('Переписка:');
  const sidecarPath = (await notice.textContent())!.match(/Переписка: (.+)$/)![1];
  const data = JSON.parse(readFileSync(sidecarPath, 'utf8'));
  expect(data.version).toBe(2);
  const sourcePath = resolve(dirname(sidecarPath), data.mesh.relative_path);
  await page.getByRole('button', { name: 'Новый проект', exact: true }).click();
  await page.getByTestId('open-conversation-input').setInputFiles(sidecarPath);
  await expect(page.getByRole('dialog', { name: 'Исходник сетки проекта' })).toBeVisible();
  await page.getByLabel('Связанный STL/OBJ').setInputFiles(sourcePath);
  await expect(page.getByRole('dialog', { name: 'Исходник сетки проекта' })).toHaveCount(0);
  await page.getByText('Эталон STL/OBJ', { exact: false }).click();
  await expect(page.getByTestId('mesh-state')).toContainText('Готов');
  await expect(page.getByRole('region', { name: 'Соответствие сетке' })).toContainText('Не подтверждено');
});

for (const mode of ['история', 'исходник']) {
test(`отмена импорта запрещает позднему Open переключить проект: ${mode}`, async ({ page }) => {
  await prepare(page);
  await page.getByLabel('Сообщение', { exact: true }).fill('Черновик текущего проекта');
  let release!: () => void;
  const barrier = new Promise<void>(resolve => { release = resolve; });
  let started!: () => void;
  const entered = new Promise<void>(resolve => { started = resolve; });
  await page.route('**/api/conversation-imports', route => route.fulfill({ json: { import_id: 'staged' } }));
  await page.route('**/api/conversation-imports/staged/*', route => route.fulfill({ status: 204 }));
  await page.route('**/api/conversations/open', async route => {
    started(); await barrier;
    await route.fulfill({ json: { session_id: 'late-import', runs: [], document: {} } }).catch(() => {});
  });
  await page.getByTestId('open-conversation-input').setInputFiles({ name: 'saved.cadnext.json',
    mimeType: 'application/json', buffer: Buffer.from(JSON.stringify({ version: 2,
      mesh: { reference: { sha256: 'a'.repeat(64) }, relative_path: `part.cadnext.assets/${'a'.repeat(64)}.obj` } })) });
  if (mode === 'история') await page.getByRole('button', { name: 'Открыть только историю' }).click();
  else await page.getByLabel('Связанный STL/OBJ').setInputFiles(mesh);
  await entered;
  await page.getByRole('dialog', { name: 'Исходник сетки проекта' }).getByRole('button', { name: 'Отмена' }).click();
  const response = page.waitForResponse(r => r.url().endsWith('/api/conversations/open')).catch(() => null);
  release();
  await response;
  await expect(page.getByLabel('Сообщение', { exact: true })).toHaveValue('Черновик текущего проекта');
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem('cadnext.v1.session'))).not.toBe('late-import');
});
}
