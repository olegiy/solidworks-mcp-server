import { expect, test } from '@playwright/test';

test('отправка ждёт уровней и не передаёт устаревшее отключение обязательного рассуждения', async ({ page }) => {
  const profile = await (await page.request.post('/api/profiles', { data: {
    name: 'Ожидание уровней', base_url: 'https://another-provider.test/v1',
    model: 'fake-model', reasoning_effort: 'none',
  } })).json();
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/api/reasoning?**', async route => {
    await held;
    await route.fulfill({ json: { status: 'matched', model_id: 'fake-model', reasoning: {
      mandatory: true, default_effort: 'high', supported_efforts: ['low', 'high'],
    } } });
  });
  await page.addInitScript(id => {
    localStorage.setItem('cadnext.v1.profile', id);
    localStorage.setItem('cadnext.v1.session', 'delayed-reasoning-test');
    localStorage.setItem('cadnext.v1.goalOptions', JSON.stringify({ enabled: false,
      target_similarity: 100, max_evaluations: 10, max_no_progress: 3 }));
  }, profile.id);
  await page.goto('/');
  await page.getByLabel('Сообщение', { exact: true }).fill('Прочитай размер');
  await expect(page.getByRole('button', { name: 'Отправить', exact: true })).toBeDisabled();
  release();
  await expect(page.locator('.composer').getByRole('combobox', { name: 'Сила рассуждения', exact: true }))
    .toHaveValue('high');
  await page.getByRole('button', { name: 'Отправить', exact: true }).click();
  await expect(page.getByText('Размер 62 мм. Готово.', { exact: true })).toBeVisible();
  expect((await (await page.request.get(`/api/profiles/${profile.id}`)).json()).reasoning_effort).toBe('high');
});

test('уровни OpenRouter автоматически загружаются для другого API и открытых настроек', async ({ page }) => {
  const profile = await (await page.request.post('/api/profiles', { data: {
    name: 'Прямой DeepSeek', base_url: 'https://another-provider.test/v1',
    model: 'deepseek-v4.1-flash-1017', reasoning_effort: 'max',
  } })).json();
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/api/reasoning?**', async route => {
    await held;
    await route.fulfill({ json: { status: 'matched', model_id: 'deepseek/deepseek-v4.1-flash-1017',
      reasoning: { mandatory: false, default_effort: 'high', supported_efforts: ['max', 'high', 'low', 'none'] } } });
  });
  await page.addInitScript(id => localStorage.setItem('cadnext.v1.profile', id), profile.id);
  await page.goto('/');
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  release();
  const expected = ['По умолчанию API', 'Максимальная', 'Высокая', 'Низкая', 'Без рассуждения'];
  await expect(page.locator('.composer').getByRole('combobox', { name: 'Сила рассуждения', exact: true })
    .locator('option')).toHaveText(expected);
  await expect(page.getByRole('dialog', { name: 'Настройки', exact: true })
    .getByRole('combobox', { name: 'Сила рассуждения', exact: true }).locator('option')).toHaveText(expected);
  await expect(page.getByRole('dialog').getByText(/Уровни из OpenRouter/)).toBeVisible();
});

test('неизвестный псевдоним показывает причину стандартного списка уровней', async ({ page }) => {
  const profile = await (await page.request.post('/api/profiles', { data: {
    name: 'Псевдоним', base_url: 'https://another-provider.test/v1', model: 'custom-highspeed',
  } })).json();
  await page.route('**/api/reasoning?**', route => route.fulfill({ json: {
    status: 'not_found', model_id: null, reasoning: null,
  } }));
  await page.addInitScript(id => localStorage.setItem('cadnext.v1.profile', id), profile.id);
  await page.goto('/');
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  await expect(page.getByRole('dialog').getByText(/Соответствие в OpenRouter не найдено/)).toBeVisible();
});

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
  const reasoning = page.getByRole('dialog', { name: 'Настройки', exact: true }).getByRole('combobox', { name: 'Сила рассуждения', exact: true });
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

test('OpenRouter каталог показывает уровни рассуждения выбранной модели', async ({ page }) => {
  await page.route('**/api/models', async route => {
    await route.fulfill({ json: { models: [{ id: 'deepseek/test', reasoning: {
      mandatory: false, default_effort: 'high', supported_efforts: ['max', 'high', 'low', 'none'],
    } }] } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  await page.getByRole('button', { name: 'Новое подключение' }).click();
  await page.getByLabel('Адрес API').fill('https://openrouter.ai/api/v1');
  const model = page.getByRole('combobox', { name: 'Имя модели', exact: true });
  await model.click();
  await page.getByRole('option', { name: 'deepseek/test', exact: true }).click();
  const reasoning = page.getByRole('dialog', { name: 'Настройки', exact: true })
    .getByRole('combobox', { name: 'Сила рассуждения', exact: true });
  await expect(reasoning.locator('option')).toHaveCount(5);
  await expect(reasoning.locator('option')).toHaveText([
    'По умолчанию API', 'Максимальная', 'Высокая', 'Низкая', 'Без рассуждения',
  ]);
});

test('обязательное рассуждение OpenRouter не предлагает отключение', async ({ page }) => {
  await page.route('**/api/models', async route => {
    await route.fulfill({ json: { models: [{ id: 'mandatory/test', reasoning: {
      mandatory: true, default_effort: 'high', supported_efforts: ['low', 'high', 'none', 'provider-special'],
    } }] } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  await page.getByRole('button', { name: 'Новое подключение' }).click();
  await page.getByLabel('Адрес API').fill('https://openrouter.ai/api/v1');
  const model = page.getByRole('combobox', { name: 'Имя модели', exact: true });
  await model.click();
  await page.getByRole('option', { name: 'mandatory/test', exact: true }).click();
  await page.getByLabel('Название подключения').fill('Mandatory reasoning');
  const reasoning = page.getByRole('dialog', { name: 'Настройки', exact: true })
    .getByRole('combobox', { name: 'Сила рассуждения', exact: true });
  await expect(reasoning.locator('option')).toHaveText(['Низкая', 'Высокая']);
  await expect(reasoning).toHaveValue('high');
  await page.getByRole('button', { name: 'Сохранить подключение' }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Подключение сохранено' })).toBeVisible();
  const saved = await (await page.request.get('/api/profiles')).json();
  expect(saved.find((profile: { name: string }) => profile.name === 'Mandatory reasoning').reasoning_effort).toBe('high');
});

test('смена модели OpenRouter сбрасывает неподдерживаемый сохранённый уровень', async ({ page }) => {
  await page.route('**/api/models', async route => {
    await route.fulfill({ json: { models: [
      { id: 'first/test', reasoning: { mandatory: false, default_effort: 'high', supported_efforts: ['high', 'low'] } },
      { id: 'second/test', reasoning: { mandatory: false, default_effort: 'low', supported_efforts: ['low', 'none'] } },
    ] } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  await page.getByRole('button', { name: 'Новое подключение' }).click();
  await page.getByLabel('Адрес API').fill('https://openrouter.ai/api/v1');
  const model = page.getByRole('combobox', { name: 'Имя модели', exact: true });
  await model.click();
  await page.getByRole('option', { name: 'first/test', exact: true }).click();
  const reasoning = page.getByRole('dialog', { name: 'Настройки', exact: true })
    .getByRole('combobox', { name: 'Сила рассуждения', exact: true });
  await reasoning.selectOption('high');
  await model.click();
  await page.getByRole('option', { name: 'second/test', exact: true }).click();
  await expect(reasoning).toHaveValue('');
  await expect(reasoning.locator('option')).toHaveText(['По умолчанию API', 'Низкая', 'Без рассуждения']);
  await model.click();
  await page.getByRole('option', { name: 'first/test', exact: true }).click();
  await reasoning.selectOption('high');
  await model.fill('custom-manual-model');
  await expect(reasoning).toHaveValue('');
});

test('сохранённое OpenRouter подключение загружает уровни для нижней панели чата', async ({ page }) => {
  const created = await page.request.post('/api/profiles', { data: {
    name: 'OpenRouter уровни', base_url: 'https://openrouter.ai/api/v1', model: 'deepseek/test',
  } });
  const profile = await created.json();
  await page.route('**/api/reasoning?**', async route => {
    await route.fulfill({ json: { status: 'matched', model_id: 'deepseek/test', reasoning: {
      mandatory: false, default_effort: 'high', supported_efforts: ['max', 'high', 'low', 'none'],
    } } });
  });
  await page.addInitScript(id => localStorage.setItem('cadnext.v1.profile', id), profile.id);
  await page.goto('/');
  await expect(page.getByLabel('Активное подключение')).toHaveValue(profile.id);
  const reasoning = page.locator('.composer').getByRole('combobox', { name: 'Сила рассуждения', exact: true });
  await expect(reasoning.locator('option')).toHaveText([
    'По умолчанию API', 'Максимальная', 'Высокая', 'Низкая', 'Без рассуждения',
  ]);
});

test('старый обязательный уровень нормализуется при сохранении и перед отправкой', async ({ page }) => {
  const profile = await (await page.request.post('/api/profiles', { data: {
    name: 'Старый обязательный', base_url: 'https://openrouter.ai/api/v1',
    model: 'fake-model', reasoning_effort: 'none',
  } })).json();
  await page.route('**/api/reasoning?**', route => route.fulfill({ json: { status: 'matched', model_id: 'fake-model', reasoning: {
    mandatory: true, default_effort: 'high', supported_efforts: ['low', 'high'],
  } } }));
  await page.addInitScript(id => {
    localStorage.setItem('cadnext.v1.profile', id);
    localStorage.setItem('cadnext.v1.session', 'mandatory-profile-test');
    localStorage.setItem('cadnext.v1.goalOptions', JSON.stringify({ enabled: false,
      target_similarity: 100, max_evaluations: 10, max_no_progress: 3 }));
  }, profile.id);
  await page.goto('/');
  const reasoning = page.locator('.composer').getByRole('combobox', { name: 'Сила рассуждения', exact: true });
  await expect(reasoning).toHaveValue('high');
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  await page.getByRole('button', { name: 'Сохранить подключение' }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Подключение сохранено' })).toBeVisible();
  expect((await (await page.request.get(`/api/profiles/${profile.id}`)).json()).reasoning_effort).toBe('high');
  await page.getByRole('button', { name: 'Закрыть настройки' }).click();
  await page.request.put(`/api/profiles/${profile.id}`, { data: { reasoning_effort: 'none' } });
  await page.reload();
  await expect(reasoning).toHaveValue('high');
  await page.getByLabel('Сообщение', { exact: true }).fill('Прочитай размер');
  let sentEffort: string | null = null;
  await page.route('**/api/runs', async route => {
    sentEffort = (await (await page.request.get(`/api/profiles/${profile.id}`)).json()).reasoning_effort;
    await route.continue();
  });
  await page.getByRole('button', { name: 'Отправить', exact: true }).click();
  await expect(page.getByText('Размер 62 мм. Готово.', { exact: true })).toBeVisible();
  expect(sentEffort).toBe('high');
});

test('каталог несохранённого адреса не меняет уровни активного подключения', async ({ page }) => {
  const profile = await (await page.request.post('/api/profiles', { data: {
    name: 'Исходный каталог', base_url: 'https://openrouter.ai/api/v1', model: 'fake-model',
  } })).json();
  await page.route('**/api/reasoning?**', route => route.fulfill({ json: {
    status: 'matched', model_id: 'fake-model', reasoning: {
      mandatory: false, default_effort: 'high', supported_efforts: ['high'],
    },
  } }));
  await page.route('**/api/models**', async route => {
    const draft = route.request().method() === 'POST'
      && route.request().postDataJSON().base_url === 'https://draft.test/v1';
    await route.fulfill({ json: { models: [{ id: 'fake-model', reasoning: {
      mandatory: false, default_effort: 'high', supported_efforts: draft ? ['max'] : ['high'],
    } }] } });
  });
  await page.addInitScript(id => localStorage.setItem('cadnext.v1.profile', id), profile.id);
  await page.goto('/');
  const reasoning = page.locator('.composer').getByRole('combobox', { name: 'Сила рассуждения', exact: true });
  await expect(reasoning.locator('option')).toHaveText(['По умолчанию API', 'Высокая']);
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  await page.getByLabel('Адрес API').fill('https://draft.test/v1');
  await page.getByRole('combobox', { name: 'Имя модели', exact: true }).click();
  await page.getByRole('option', { name: 'fake-model', exact: true }).click();
  await page.getByRole('button', { name: 'Закрыть настройки' }).click();
  await expect(reasoning.locator('option')).toHaveText(['По умолчанию API', 'Высокая']);
});

test('смена сервера удаляет прежний ключ и объясняет это пользователю', async ({ page }) => {
  const created = await page.request.post('/api/profiles', { data: {
    name: 'Смена сервера', base_url: 'https://first.example.test/v1', model: 'fake-model',
    api_key: 'audit-test-key',
  } });
  const profile = await created.json();
  expect(profile.has_key).toBe(true);
  expect(profile).not.toHaveProperty('api_key');
  await page.addInitScript(id => localStorage.setItem('cadnext.v1.profile', id), profile.id);
  await page.goto('/');
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  await page.getByLabel('Адрес API').fill('https://second.example.test/v1');
  await page.getByRole('button', { name: 'Сохранить подключение' }).click();
  await expect(page.getByRole('status').filter({ hasText: 'прежний ключ удалён' })).toBeVisible();
  const saved = await (await page.request.get(`/api/profiles/${profile.id}`)).json();
  expect(saved.has_key).toBe(false);
  expect(saved).not.toHaveProperty('api_key');
});
