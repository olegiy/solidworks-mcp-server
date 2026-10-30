import { test, expect, type Page } from '@playwright/test';

const PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
  'base64');

async function connection(page: Page, name: string, vision = true) {
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  await page.getByRole('button', { name: 'Новое подключение' }).click();
  await page.getByLabel('Название подключения').fill(name);
  await page.getByLabel('Адрес API').fill('http://localhost/v1');
  await page.getByLabel('Имя модели').fill('fake-model');
  if (vision) await page.getByLabel('Поддержка изображений').check();
  await page.getByRole('button', { name: 'Сохранить подключение' }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Подключение сохранено' })).toBeVisible();
  await page.getByRole('button', { name: 'Закрыть настройки' }).click();
}

async function send(page: Page, text: string) {
  await page.getByLabel('Сообщение').fill(text);
  await page.getByRole('button', { name: 'Отправить', exact: true }).click();
}

test('оценка настраивается только в настройках и сохраняется для следующего запроса', async ({ page }) => {
  await page.goto('/');
  await connection(page, 'Параметры оценки');
  await expect(page.getByRole('form', { name: 'Отправка сообщения' }))
    .not.toContainText('Оценка результата');
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  await page.getByLabel('Целевая схожесть, %').fill('90');
  await page.getByLabel('Максимум оценок', { exact: true }).fill('5');
  await page.getByLabel('Исправлений без прогресса', { exact: true }).fill('2');
  await page.getByRole('button', { name: 'Закрыть настройки', exact: true }).click();
  await page.reload();
  const request = page.waitForRequest(request => request.method() === 'POST'
    && request.url().endsWith('/api/runs'));
  await send(page, 'оценка-100');
  expect((await request).postDataJSON().evaluation).toEqual({
    enabled: true, target_similarity: 90, max_evaluations: 5, max_no_progress: 2 });
  await expect(page.getByTestId('goal-card').last().getByText('Цель достигнута', { exact: true })).toBeVisible();
});

test('серверная оценка показывает 100% и зафиксированные требования', async ({ page }) => {
  await page.goto('/');
  await connection(page, 'Цель 100');
  await send(page, 'оценка-100');
  await expect(page.getByTestId('run-status').last()).toHaveText('Завершён');
  const card = page.getByTestId('goal-card').last();
  await expect(card.getByText('100%', { exact: true })).toBeVisible();
  await expect(card.getByText('Цель достигнута', { exact: true })).toBeVisible();
  await expect(card.getByText('Требования зафиксированы до изменений.')).toBeVisible();
  await expect(card.getByTestId('goal-requirement-d1')).toContainText('Выполнено');
  await expect(card.getByTestId('goal-requirement-d1')).toContainText('CAD-проверка');
});

test('неудача 70% исправляется и показывает вторую оценку', async ({ page }) => {
  await page.goto('/');
  await connection(page, 'Цель 70');
  await send(page, 'оценка-70');
  await expect(page.getByTestId('goal-card').last().getByText('100%', { exact: true })).toBeVisible();
  await expect(page.getByTestId('goal-card').last().getByText('Оценка 2 из 10')).toBeVisible();
  await expect(page.getByTestId('goal-card').last().getByText('Цель достигнута', { exact: true })).toBeVisible();
});

test('уточнение не показывается как выполненное построение', async ({ page }) => {
  await page.goto('/');
  await connection(page, 'Уточнение');
  await send(page, 'уточнение радиуса');
  await expect(page.getByTestId('run-status').last()).toHaveText('Нужно уточнение');
  const indicator = page.getByRole('region', { name: 'Ход выполнения', exact: true });
  await expect(indicator.getByRole('status')).toHaveText('Нужно уточнение');
  const clarification = page.getByRole('alert', { name: 'Нужен ваш ответ' });
  await expect(clarification).toBeVisible();
  await expect(clarification.getByRole('heading', { name: 'Нужен ваш ответ' })).toBeInViewport();
  await expect(clarification.getByRole('listitem')).toHaveCount(2);
  await expect(clarification.getByRole('listitem').first())
    .toHaveText('Какой радиус скругления использовать, в мм?');
  await expect(clarification).toContainText('Для продолжения ответьте на вопросы в поле сообщения.');
  await clarification.getByRole('button', { name: 'Ответить', exact: true }).click();
  await expect(page.getByLabel('Сообщение', { exact: true })).toBeFocused();
  await expect(page.getByLabel('Сообщение', { exact: true })).toBeEmpty();
  await expect(page.getByTestId('goal-card').last().getByText('Нужно уточнение', { exact: true })).toBeVisible();
  await expect(page.getByText('Вызовов: 0', { exact: true }).last()).toBeVisible();
  await indicator.getByRole('button', { name: 'Подробности выполнения' }).click();
  await expect(indicator).toContainText('CAD-операций завершено: 0 из 0');
  await page.reload();
  await expect(page.getByTestId('run-status').last()).toHaveText('Нужно уточнение');
  await expect(indicator.getByRole('status')).toHaveText('Нужно уточнение');
  await expect(clarification.getByRole('listitem')).toHaveCount(2);

  // Older saved conversations have no clarification metadata on the message event.
  await page.route('**/api/runs?session_id=*', async route => {
    const response = await route.fetch();
    const runs = await response.json();
    for (const run of runs) for (const event of run.events) {
      delete event.data.kind;
      delete event.data.questions;
    }
    await route.fulfill({ response, json: runs });
  });
  await page.reload();
  await expect(clarification.getByRole('listitem')).toHaveCount(2);
  await page.getByLabel('Язык интерфейса').selectOption('en');
  await expect(page.getByTestId('run-status').last()).toHaveText('Clarification required');
  await expect(page.getByRole('region', { name: 'Execution progress', exact: true })
    .getByRole('status')).toHaveText('Clarification required');
  const englishClarification = page.getByRole('alert', { name: 'Your answer is needed' });
  await expect(englishClarification).toBeVisible();
  await englishClarification.getByRole('button', { name: 'Reply', exact: true }).click();
  await expect(page.getByLabel('Message', { exact: true })).toBeFocused();
});

test('виден визуальный pass, но неверный размер не даёт успеха', async ({ page }) => {
  await page.goto('/');
  await connection(page, 'Визуал');
  await page.getByLabel('Файлы изображений').setInputFiles({
    name: 'reference.png', mimeType: 'image/png', buffer: PNG });
  await send(page, 'визуал');
  await expect(page.getByTestId('run-status').last()).toHaveText('Достигнут лимит');
  const card = page.getByTestId('goal-card').last();
  await expect(card.getByText('Цель не достигнута', { exact: true })).toBeVisible();
  await expect(card.getByText(/не доказывает скрытую геометрию/)).toBeVisible();
  await expect(card.getByTestId('goal-requirement-v1')).toContainText('Выполнено');
  await expect(card.getByTestId('goal-requirement-v1')).toContainText('Оценка модели');
  await expect(card.getByTestId('goal-requirement-d1')).toContainText('Не выполнено');
  await expect(card.getByText('Цель достигнута', { exact: true })).toHaveCount(0);
});

test('отсутствие прогресса упирается в лимит, а не повторяет запросы', async ({ page }) => {
  await page.goto('/');
  await connection(page, 'Нет прогресса');
  await send(page, 'нет-прогресса');
  await expect(page.getByTestId('run-status').last()).toHaveText('Достигнут лимит');
  await expect(page.getByTestId('goal-card').last()).toContainText('нет прогресса');
  await expect(page.getByText('Оценка 1 из 10')).toBeVisible();
});

test('явное отключение оценки возвращает старый режим без карточки', async ({ page }) => {
  await page.goto('/');
  await connection(page, 'Без цели');
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  await page.getByLabel('Проверять цель до достижения').uncheck();
  await page.getByRole('button', { name: 'Закрыть настройки', exact: true }).click();
  await send(page, 'оценка-100');
  await expect(page.getByTestId('run-status').last()).toHaveText('Завершён');
  await expect(page.getByTestId('goal-card')).toHaveCount(0);
});

test('явная Vision-маршрутизация доводит цель и показывает эффективный профиль', async ({ page }) => {
  const text = await (await page.request.post('/api/profiles', { data: {
    name: 'Текст', base_url: 'http://localhost/v1', model: 'fake-model', vision: false,
  } })).json();
  const vision = await (await page.request.post('/api/profiles', { data: {
    name: 'Vision', base_url: 'https://vision.example.test/v1', model: 'fake-vision', vision: true,
  } })).json();
  await page.goto('/');
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  await page.getByLabel('Vision-профиль').selectOption(vision.id);
  await expect(page.getByRole('status').filter({ hasText: 'Маршрутизация сохранена' })).toHaveCount(0);
  await expect(page.getByText('Подтвердите согласие на автоматическую отправку этому API.')).toBeVisible();
  await page.getByLabel('Разрешить отправку текста, истории и необезличенных изображений этому API').click();
  await expect(page.getByRole('status').filter({ hasText: 'Маршрутизация сохранена' })).toBeVisible();
  await page.getByRole('button', { name: 'Закрыть настройки' }).click();
  await page.getByLabel('Активное подключение').selectOption(text.id);
  await expect(page.getByText(/Фактически:/)).toBeVisible();
  await send(page, 'оценка-100');
  await expect(page.getByTestId('run-status').last()).toHaveText('Завершён');
  await expect(page.getByTestId('goal-card').last().getByText('Цель достигнута', { exact: true })).toBeVisible();
});

test('без согласованного Vision-профиля отправка заблокирована с подсказкой', async ({ page }) => {
  await page.request.put('/api/settings', { data: { vision_routing_approved: false } });
  const text = await (await page.request.post('/api/profiles', { data: {
    name: 'Текст', base_url: 'http://localhost/v1', model: 'fake-model', vision: false,
  } })).json();
  await page.goto('/');
  await page.getByLabel('Активное подключение').selectOption(text.id);
  await expect(page.getByText('Настройте Vision-профиль или отключите оценку.')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Отправить', exact: true })).toBeDisabled();
});

test('остановка до оценки не маскируется под завершённую цель', async ({ page }) => {
  await page.goto('/');
  await connection(page, 'Остановка цели');
  await send(page, 'Читай долго');
  await expect(page.getByText('Вызовов: 1', { exact: true }).last()).toBeVisible();
  await page.getByRole('button', { name: 'Остановить', exact: true }).click();
  await expect(page.getByTestId('run-status').last()).toHaveText('Остановлен');
  await expect(page.getByTestId('goal-card').last().locator('.badge')).toHaveText('Остановлен');
  await expect(page.getByTestId('goal-card').last().getByText('Завершено', { exact: true })).toHaveCount(0);
});
