import { test, expect, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';

async function connection(page: Page, name: string, remote = false) {
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  await page.getByRole('button', { name: 'Новое подключение' }).click();
  await page.getByLabel('Название подключения').fill(name);
  await page.getByLabel('Адрес API').fill(remote ? 'https://example.test/v1' : 'http://localhost/v1');
  await page.getByLabel('Имя модели').fill('fake-model');
  await page.getByLabel('Поддержка изображений').check();
  if (remote) await page.getByLabel('Локальное подключение').uncheck();
  await page.getByRole('button', { name: 'Сохранить подключение' }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Подключение сохранено' })).toBeVisible();
  await page.getByRole('button', { name: 'Закрыть настройки' }).click();
  await expect(page.getByLabel('Активное подключение')).toHaveValue(/.+/);
}

async function disableGoal(page: Page) {
  await page.locator('.goal-inline summary').click();
  await page.getByLabel('Проверять цель до достижения').uncheck();
  await page.locator('.goal-inline summary').click();
}

async function send(page: Page, text: string) {
  await page.getByLabel('Сообщение').fill(text);
  await page.getByRole('button', { name: 'Отправить', exact: true }).click();
}

test('сохранить подключение, получить CAD-результат и снимок, восстановить после обновления', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'CAD-Agent-Next' })).toBeVisible();
  await connection(page, 'Локальная модель');
  await send(page, 'Прочитай размер');
  await expect(page.getByText('Размер 62 мм. Готово.', { exact: true })).toBeVisible();
  await expect(page.getByTestId('run-status').last()).toHaveText('Завершён');
  await page.reload();
  await expect(page.getByText('Размер 62 мм. Готово.', { exact: true })).toBeVisible();
  await send(page, 'Покажи снимок');
  const screenshot = page.getByRole('img', { name: 'Снимок SolidWorks' });
  await expect(screenshot).toBeVisible();
  const box = (await screenshot.boundingBox())!;
  expect(box.width).toBeLessThanOrEqual(400);
  expect(box.height).toBeLessThanOrEqual(300);
  await page.getByRole('button', { name: 'Журнал операций' }).last().click();
  await expect(page.getByText('screenshot', { exact: true })).toBeVisible();
});

test('кнопка сохранения пишет переписку, а Open возвращает её', async ({ page }) => {
  await page.goto('/');
  await connection(page, 'Сохранение');
  await send(page, 'Прочитай размер');
  await expect(page.getByText('Размер 62 мм. Готово.', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Сохранить проект', exact: true }).click();
  const notice = page.getByTestId('save-notice');
  await expect(notice).toContainText('Сохранено:');
  await expect(notice).toContainText('Переписка:');
  const text = (await notice.textContent()) ?? '';
  const match = text.match(/Переписка: (.+)$/);
  expect(match).not.toBeNull();
  const conversationPath = match![1];
  const saved = JSON.parse(readFileSync(conversationPath, 'utf8'));
  expect(saved.schema).toBe('cadnext.conversation');
  expect(saved.messages.length).toBeGreaterThan(0);
  expect(saved.runs.length).toBeGreaterThan(0);

  await page.getByRole('button', { name: 'Новый диалог', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Что создадим сегодня?' })).toBeVisible();
  await page.getByTestId('open-conversation-input').setInputFiles(conversationPath);
  await expect(page.getByText('Прочитай размер', { exact: true })).toBeVisible();
  await expect(page.getByText('Размер 62 мм. Готово.', { exact: true })).toBeVisible();
  await expect(page.getByRole('status').filter({ hasText: 'Диалог загружен' })).toBeVisible();
});

test('разрешение Python постоянно для новых диалогов и отзывается в настройках', async ({ page }) => {
  await page.request.put('/api/settings', { data: { python_execution_allowed: false } });
  await page.goto('/');
  await connection(page, 'Код');
  await send(page, 'Выполни код');
  const dialog = page.getByRole('dialog', { name: 'Подтверждение Python-кода' });
  await expect(dialog.getByText('result = 62\nprint(result)', { exact: true })).toBeVisible();
  await dialog.getByRole('button', { name: 'Разрешить постоянно' }).click();
  await expect(page.getByTestId('run-status').last()).toHaveText('Завершён');
  await expect(page.getByText('Результат требует проверки', { exact: true }).last()).toBeVisible();
  await send(page, 'Ещё код');
  await expect(page.getByTestId('run-status').last()).toHaveText('Завершён');
  await expect(dialog).toHaveCount(0);
  await page.reload();
  await send(page, 'Продолжи код');
  await expect(page.getByTestId('run-status').last()).toHaveText('Завершён');
  await expect(dialog).toHaveCount(0);
  await page.getByRole('button', { name: 'Новый диалог', exact: true }).click();
  await send(page, 'Другой код');
  await expect(page.getByTestId('run-status').last()).toHaveText('Завершён');
  await expect(dialog).toHaveCount(0);
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  const permission = page.getByRole('checkbox', { name: 'Всегда разрешать выполнение Python' });
  await expect(permission).toBeChecked();
  await permission.uncheck();
  await expect(permission).not.toBeChecked();
  await expect(permission).toBeEnabled();
  await page.getByRole('button', { name: 'Закрыть настройки' }).click();
  await page.reload();
  await send(page, 'После отзыва код');
  await expect(dialog).toBeVisible();
  await dialog.getByRole('button', { name: 'Отклонить' }).click();
  await expect(page.getByTestId('run-status').last()).toHaveText('Отклонён');
  await expect(page.getByText('Вызовов: 0', { exact: true }).last()).toBeVisible();
});

test('остановка во время CAD и обновление страницы не запускают следующий вызов', async ({ page }) => {
  await page.goto('/');
  await connection(page, 'Остановка');
  await send(page, 'Читай долго');
  await expect(page.getByText('Вызовов: 1', { exact: true }).last()).toBeVisible();
  await page.getByRole('button', { name: 'Остановить', exact: true }).click();
  await expect(page.getByTestId('run-status').last()).toHaveText('Остановка');
  await page.reload();
  await expect(page.getByTestId('run-status').last()).toHaveText('Остановлен');
  await expect(page.getByText('Вызовов: 1', { exact: true }).last()).toBeVisible();
});

test('облачные данные отправляются без подтверждения, узкий экран доступен', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  await connection(page, 'Облако', true);
  await send(page, 'Покажи снимок');
  await expect(page.getByRole('img', { name: 'Снимок SolidWorks' })).toBeVisible();
  await expect(page.getByTestId('run-status').last()).toHaveText('Завершён');
  await expect(page.getByRole('dialog', { name: 'Передача данных провайдеру' })).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
  await expect(page.getByLabel('Сообщение')).toBeInViewport();
});

test('закрытие вкладки запрещает следующий шаг, новая вкладка показывает существующий запрос', async ({ page, context }) => {
  await page.goto('/');
  await connection(page, 'Закрытие');
  await send(page, 'Читай долго');
  await expect(page.getByText('Вызовов: 1', { exact: true }).last()).toBeVisible();
  await page.close();
  const next = await context.newPage();
  await next.goto('/');
  await expect(next.getByTestId('run-status').last()).toHaveText('Остановлен');
  await expect(next.getByText('Вызовов: 1', { exact: true }).last()).toBeVisible();
});

test('закрытие вкладки до ответа о запуске также останавливает CAD', async ({ page, context }) => {
  await page.goto('/');
  await connection(page, 'Медленный ответ');
  let runId = '';
  await page.route('**/api/runs', async route => {
    if (route.request().method() !== 'POST') return route.continue();
    const response = await route.fetch();
    runId = (await response.json()).id;
    // Deliberately withhold the HTTP response so the tab cannot open its SSE yet.
    await new Promise(resolve => setTimeout(resolve, 3000));
    await route.fulfill({ response }).catch(() => {});
  });
  await send(page, 'Читай долго');
  await expect.poll(() => runId).not.toBe('');
  await page.close();
  await expect.poll(async () => (await context.request.get(`/api/runs/${runId}`)).json()
    .then(run => run.status)).toBe('stopped');
  const run = await (await context.request.get(`/api/runs/${runId}`)).json();
  expect(run.call_count).toBeLessThanOrEqual(1);
});

test('восстановление обновляет незавершённый снимок, даже если health уже видит завершение', async ({ page, context }) => {
  await page.goto('/');
  await connection(page, 'Восстановление');
  const profileId = await page.getByLabel('Активное подключение').inputValue();
  await page.evaluate(() => localStorage.setItem('cadnext.v1.session', 'restore-race'));
  await context.request.post('/api/runs', { data: {
    session_id: 'restore-race', profile_id: profileId, message: 'Читай долго',
  } });
  await page.route('**/api/health', async route => {
    await new Promise(resolve => setTimeout(resolve, 2500));
    await route.continue();
  });
  await page.reload();
  await expect(page.getByTestId('run-status').last()).toHaveText('Завершён');
  await expect(page.getByRole('button', { name: 'Остановить', exact: true })).toHaveCount(0);
});

test('DNS-имя с префиксом 127 остаётся внешним провайдером без запроса согласия', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  await page.getByRole('button', { name: 'Новое подключение' }).click();
  await page.getByLabel('Название подключения').fill('Внешний DNS');
  await page.getByLabel('Адрес API').fill('https://127.example.com/v1');
  await page.getByLabel('Имя модели').fill('fake-model');
  await page.getByRole('button', { name: 'Сохранить подключение' }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Подключение сохранено' })).toBeVisible();
  await page.getByRole('button', { name: 'Закрыть настройки' }).click();
  await disableGoal(page);
  await send(page, 'Прочитай размер');
  await expect(page.getByTestId('run-status').last()).toHaveText('Завершён');
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  await expect(page.getByLabel('Локальное подключение')).not.toBeChecked();
  await expect(page.getByRole('dialog', { name: 'Передача данных провайдеру' })).toHaveCount(0);
});
