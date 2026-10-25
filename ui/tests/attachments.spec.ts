import { expect, test, type Page } from '@playwright/test';

const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAACAAAAAYCAIAAAAUMWhjAAAANElEQVR4nGNUzrjDQEvARFPTGUYtIAKMxgFBMBpEBMFoEBEEo0FEEIwGEUEwGkQEAc2DCAChPAGXw19cBgAAAABJRU5ErkJggg==', 'base64');
const picture = { name: 'деталь.png', mimeType: 'image/png', buffer: png };

async function prepare(page: Page, vision = true) {
  const result = await page.request.post('/api/profiles', { data: {
    name: 'Вложения', base_url: 'http://localhost/v1', model: 'fake-model', vision,
  } });
  const profile = await result.json();
  await page.addInitScript(id => {
    if (sessionStorage.getItem('attachments.initialized')) return;
    localStorage.setItem('cadnext.v1.profile', id);
    localStorage.setItem('cadnext.v1.session', crypto.randomUUID());
    sessionStorage.setItem('attachments.initialized', '1');
  }, profile.id);
  await page.goto('/');
  await expect(page.getByLabel('Активное подключение')).toHaveValue(profile.id);
  return profile;
}

test('модель и рассуждения рядом с отправкой внизу, выбор сохраняется и попадает в запрос', async ({ page }) => {
  const profile = await prepare(page);
  const composer = page.getByRole('form', { name: 'Отправка сообщения' });
  const reasoning = composer.getByRole('combobox', { name: 'Сила рассуждения', exact: true });
  for (const width of [1280, 390]) {
    await page.setViewportSize({ width, height: 844 });
    await expect(composer.getByLabel('Активное подключение')).toBeInViewport();
    await expect(reasoning).toBeInViewport();
    await expect(page.getByRole('button', { name: 'Отправить', exact: true })).toBeInViewport();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  await reasoning.selectOption('high');
  await expect.poll(async () => (await (await page.request.get(`/api/profiles/${profile.id}`)).json()).reasoning_effort).toBe('high');
  await page.reload();
  await expect(reasoning).toHaveValue('high');
  await page.getByLabel('Сообщение', { exact: true }).fill('Прочитай размер');
  await page.getByRole('button', { name: 'Отправить', exact: true }).click();
  await expect(page.getByText('Размер 62 мм. Готово.', { exact: true })).toBeVisible();
});

test('скрепка открывает выбор, предпросмотр удаляется и повторное прикрепление передаёт картинку модели', async ({ page }) => {
  await prepare(page);
  const chooser = page.waitForEvent('filechooser');
  await page.getByRole('button', { name: 'Прикрепить изображение', exact: true }).click();
  await (await chooser).setFiles(picture);
  const preview = page.getByRole('region', { name: 'Вложения к сообщению' });
  await expect(preview.getByRole('img', { name: 'деталь.png', exact: true })).toBeVisible();
  expect(await preview.getByRole('img').evaluate((el: HTMLImageElement) => el.naturalWidth)).toBe(32);
  await preview.getByRole('button', { name: 'Удалить деталь.png', exact: true }).click();
  await expect(preview).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Отправить', exact: true })).toBeDisabled();
  await page.getByLabel('Файлы изображений').setInputFiles(picture);
  await page.getByLabel('Сообщение', { exact: true }).fill('Проверь вложение');
  await page.getByRole('button', { name: 'Отправить', exact: true }).click();
  await expect(page.getByText('Получено изображений: 1. Размер: 32 × 24.', { exact: true })).toBeVisible();
  await expect(preview).toHaveCount(0);
  await expect(page.locator('.message.user').getByRole('img', { name: 'деталь.png' })).toBeVisible();
  await page.reload();
  await expect(page.locator('.message.user').getByRole('img', { name: 'деталь.png' })).toBeVisible();
});

test('можно отправить несколько изображений без текста', async ({ page }) => {
  await prepare(page);
  await page.getByLabel('Файлы изображений').setInputFiles([picture, { ...picture, name: 'вид.png' }]);
  await expect(page.getByRole('region', { name: 'Вложения к сообщению' }).getByRole('img')).toHaveCount(2);
  await page.getByRole('button', { name: 'Отправить', exact: true }).click();
  await expect(page.getByText('Получено изображений: 2. Размер: 32 × 24.', { exact: true })).toBeVisible();
});

test('текстовая модель не позволяет прикреплять или отправлять оставшиеся картинки', async ({ page }) => {
  await prepare(page);
  await page.getByLabel('Файлы изображений').setInputFiles(picture);
  const textProfile = await (await page.request.post('/api/profiles', { data: {
    name: 'Текстовая', base_url: 'http://localhost/v1', model: 'text-only', vision: false,
  } })).json();
  // Refreshing profiles through settings must preserve the unsent draft.
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  await page.getByRole('button', { name: 'Сохранить подключение' }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Подключение сохранено' })).toBeVisible();
  await page.getByRole('button', { name: 'Закрыть настройки', exact: true }).click();
  await page.getByLabel('Активное подключение').selectOption(textProfile.id);
  await expect(page.getByRole('button', { name: 'Прикрепить изображение', exact: true })).toBeDisabled();
  await expect(page.getByText('Выбранное подключение не поддерживает изображения.', { exact: true })).toBeVisible();
  await page.getByLabel('Сообщение', { exact: true }).fill('Проверь вложение');
  await expect(page.getByRole('button', { name: 'Отправить', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: 'Удалить деталь.png', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Отправить', exact: true })).toBeEnabled();
});

test('неподдерживаемый, большой и повреждённый файл не отправляются', async ({ page }) => {
  await prepare(page);
  const files = page.getByLabel('Файлы изображений');
  await files.setInputFiles({ name: 'текст.txt', mimeType: 'text/plain', buffer: Buffer.from('text') });
  await expect(page.getByRole('alert')).toContainText('PNG, JPEG или WebP');
  await files.setInputFiles({ ...picture, buffer: Buffer.alloc(2 * 1024 * 1024 + 1) });
  await expect(page.getByRole('alert')).toContainText('2 МБ');
  await files.setInputFiles({ ...picture, buffer: Buffer.from('broken image') });
  await expect(page.getByRole('alert')).toContainText('прочитать изображение');
  await expect(page.getByRole('region', { name: 'Вложения к сообщению' })).toHaveCount(0);
  await files.setInputFiles(Array.from({ length: 4 }, (_, i) => ({ ...picture, name: `${i}.png` })));
  await expect(page.getByRole('alert')).toContainText('трёх');
});

test('ошибка отправки сохраняет вложение, новый диалог очищает его', async ({ page }) => {
  await prepare(page);
  await page.getByLabel('Файлы изображений').setInputFiles(picture);
  await page.route('**/api/runs', route => route.fulfill({ status: 422, json: { detail: 'Изображение отклонено сервером.' } }));
  await page.getByRole('button', { name: 'Отправить', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('отклонено');
  await expect(page.getByRole('region', { name: 'Вложения к сообщению' }).getByRole('img')).toHaveCount(1);
  await page.getByRole('button', { name: 'Новый диалог', exact: true }).click();
  await expect(page.getByRole('region', { name: 'Вложения к сообщению' })).toHaveCount(0);
});

test('настройки не открываются посреди сохранения рассуждений и не теряют новый выбор', async ({ page }) => {
  const profile = await prepare(page);
  let started = false;
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  await page.route(`**/api/profiles/${profile.id}`, async route => {
    if (route.request().method() === 'PUT' && route.request().postDataJSON().reasoning_effort === 'high') {
      started = true;
      await held;
    }
    await route.continue();
  });
  const settings = page.getByRole('button', { name: 'Настройки', exact: true });
  try {
    await page.getByRole('form', { name: 'Отправка сообщения' })
      .getByRole('combobox', { name: 'Сила рассуждения', exact: true }).selectOption('high');
    await expect.poll(() => started).toBe(true);
    await expect(settings).toBeDisabled();
  } finally { release(); }
  await expect(settings).toBeEnabled();
  await settings.click();
  const dialog = page.getByRole('dialog', { name: 'Настройки', exact: true });
  await expect(dialog.getByRole('combobox', { name: 'Сила рассуждения', exact: true })).toHaveValue('high');
  await dialog.getByLabel('Название подключения').fill('Новое имя');
  await page.getByRole('button', { name: 'Сохранить подключение' }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Подключение сохранено' })).toBeVisible();
  expect((await (await page.request.get(`/api/profiles/${profile.id}`)).json()).reasoning_effort).toBe('high');
});
