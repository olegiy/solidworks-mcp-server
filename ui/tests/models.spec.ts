import { expect, test } from '@playwright/test';

test('новое подключение загружает модели при выборе поля и ищет без новых запросов', async ({ page }) => {
  let requests = 0;
  await page.route('**/api/models', async route => {
    requests++;
    expect(route.request().method()).toBe('POST');
    expect(route.request().postDataJSON()).toEqual({
      base_url: 'https://catalog.test/v1', api_key: 'draft-key',
    });
    await route.fulfill({ json: ['GPT-Sol-6.1', 'gpt-4.1', 'qwen3'] });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  await page.getByRole('button', { name: 'Новое подключение' }).click();
  await expect(page.getByRole('button', { name: 'Загрузить модели', exact: true })).toHaveCount(0);
  await page.getByLabel('Адрес API').fill('https://catalog.test/v1');
  await page.getByLabel('Ключ API', { exact: true }).fill('draft-key');
  const model = page.getByRole('combobox', { name: 'Имя модели', exact: true });
  const options = page.getByRole('listbox', { name: 'Модели API' }).getByRole('option');
  await model.click();
  await expect(page.getByRole('option', { name: 'qwen3', exact: true })).toBeVisible();
  await model.fill('SOL');
  await expect(options).toHaveCount(1);
  await expect(options).toHaveText('GPT-Sol-6.1');
  await model.press('ArrowDown');
  await model.press('Enter');
  await expect(model).toHaveValue('GPT-Sol-6.1');
  await expect(page.getByRole('listbox')).toHaveCount(0);
  await model.click();
  await expect(options).toHaveCount(3);
  await page.getByRole('option', { name: 'qwen3', exact: true }).click();
  await expect(model).toHaveValue('qwen3');
  expect(requests).toBe(1);
  await model.fill('custom-private-model');
  await expect(page.getByText('Модели не найдены. Можно сохранить введённое имя.')).toBeVisible();
  await model.press('Escape');
  await page.getByLabel('Название подключения').fill('Каталог с поиском');
  await page.getByRole('button', { name: 'Сохранить подключение' }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Подключение сохранено' })).toBeVisible();
  await expect(model).toHaveValue('custom-private-model');
});

test('смена адреса API отбрасывает предыдущий каталог и запоздавший ответ', async ({ page }) => {
  let oldStarted = false;
  let oldFinished = false;
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/api/models', async route => {
    const body = route.request().postDataJSON();
    if (body.base_url === 'https://old.test/v1') {
      oldStarted = true;
      await held;
      await route.fulfill({ json: ['stale-model'] }).catch(() => {});
      oldFinished = true;
    } else {
      await route.fulfill({ json: ['current-model'] });
    }
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  await page.getByRole('button', { name: 'Новое подключение' }).click();
  await page.getByLabel('Адрес API').fill('https://old.test/v1');
  const model = page.getByRole('combobox', { name: 'Имя модели', exact: true });
  await model.click();
  await expect.poll(() => oldStarted).toBeTruthy();
  await expect(page.getByRole('status').filter({ hasText: 'Загружаем модели' })).toBeVisible();
  await page.getByLabel('Адрес API').fill('https://new.test/v1');
  await model.click();
  await expect(page.getByRole('option', { name: 'current-model' })).toBeVisible();
  release();
  await expect.poll(() => oldFinished).toBeTruthy();
  await expect(page.getByRole('option', { name: 'stale-model' })).toHaveCount(0);
  await page.getByRole('option', { name: 'current-model' }).click();
  await expect(model).toHaveValue('current-model');
});

test('ошибка каталога оставляет ручной ввод и позволяет обновить список', async ({ page }) => {
  let requests = 0;
  await page.route('**/api/models', async route => {
    requests++;
    await route.fulfill(requests === 1
      ? { status: 502, json: { detail: 'API модели вернул HTTP 401. Проверьте подключение.' } }
      : { json: ['recovered-model'] });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  const model = page.getByRole('combobox', { name: 'Имя модели', exact: true });
  await model.click();
  await expect(page.getByRole('alert')).toContainText('HTTP 401');
  await model.fill('manual-model');
  await model.press('Escape');
  await expect(model).toHaveValue('manual-model');
  await page.getByRole('button', { name: 'Показать список моделей', exact: true }).click();
  await expect(page.getByRole('option', { name: 'recovered-model' })).toBeVisible();
  await expect(page.getByRole('alert')).toHaveCount(0);
  await model.press('ArrowDown');
  await model.press('Enter');
  await expect(model).toHaveValue('recovered-model');
});

test('сила рассуждения сохраняется для следующего входа и сбрасывается на настройку API', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  await page.getByRole('button', { name: 'Новое подключение' }).click();
  await page.getByLabel('Название подключения').fill('Сохранённые рассуждения');
  await page.getByLabel('Имя модели', { exact: true }).fill('fake-model');
  await page.getByLabel('Имя модели', { exact: true }).press('Escape');
  const reasoning = page.getByRole('combobox', { name: 'Сила рассуждения', exact: true });
  await expect(reasoning).toHaveValue('');
  await reasoning.selectOption('max');
  await page.getByRole('button', { name: 'Сохранить подключение' }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Подключение сохранено' })).toBeVisible();
  const selected = await page.getByLabel('Активное подключение').inputValue();
  await page.reload();
  await expect(page.getByLabel('Активное подключение')).toHaveValue(selected);
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  await expect(reasoning).toHaveValue('max');
  await reasoning.selectOption('');
  await page.getByRole('button', { name: 'Сохранить подключение' }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Подключение сохранено' })).toBeVisible();
  await page.reload();
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  await expect(reasoning).toHaveValue('');
});
