import { expect, test, type Page } from '@playwright/test';

const response = '**Проверка детали**\n\n- Толщина: 2 мм\n- Размер: 62 мм\n\n```python\nresult = 62\nprint(result)\n```\n\n[Справка](https://example.test/help)\n\n[Опасная ссылка](javascript:alert(1))\n\n<img src="https://example.test/hidden.png" onerror="alert(1)">\n\n![Внешняя картинка](https://example.test/image.png)';

async function prepare(page: Page, history = true, extraEvents: { seq: number; type: string; data: Record<string, unknown> }[] = []) {
  const result = await page.request.post('/api/profiles', { data: {
    name: 'Дизайн чата', base_url: 'http://localhost/v1', model: 'fake-model', local: true, vision: false,
  } });
  const profile = await result.json();
  await page.addInitScript(id => {
    if (sessionStorage.getItem('redesign.initialized')) return;
    localStorage.setItem('cadnext.v1.session', 'redesign');
    localStorage.setItem('cadnext.v1.profile', id);
    sessionStorage.setItem('redesign.initialized', '1');
  }, profile.id);
  if (history) await page.route('**/api/runs?session_id=redesign', route => route.fulfill({ json: [{
    id: 'design-preview', session_id: 'redesign', profile_id: profile.id,
    status: 'completed', call_count: 0, max_calls: 80, verification: 'not_required',
    confirmation: null, stop_requested: false, events: [
      { seq: 1, type: 'message', data: { role: 'user', content: 'Проверь размеры моей детали' } },
      { seq: 2, type: 'message', data: { role: 'assistant', content: response } },
      ...extraEvents,
    ],
  }] }));
  await page.goto('/');
}

test('сообщения по разным краям, поле ввода доступно на широком и узком экране', async ({ page }) => {
  await prepare(page);
  for (const width of [1280, 390]) {
    await page.setViewportSize({ width, height: 844 });
    const user = page.locator('.message.user');
    const assistant = page.locator('.message.assistant');
    await expect(user).toBeVisible();
    const left = (await assistant.boundingBox())!;
    const right = (await user.boundingBox())!;
    expect(right.x).toBeGreaterThan(left.x + 24);
    expect(right.x + right.width).toBeGreaterThan(left.x + left.width + 12);
    expect(await user.evaluate(el => getComputedStyle(el).backgroundColor))
      .not.toBe(await assistant.evaluate(el => getComputedStyle(el).backgroundColor));
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
    await expect(page.getByLabel('Сообщение', { exact: true })).toBeInViewport();
  }
});

test('ответ оформляется как Markdown, код копируется без исполнения HTML и загрузки внешних картинок', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  const requests: string[] = [];
  page.on('request', request => { if (request.url().startsWith('https://example.test/')) requests.push(request.url()); });
  await prepare(page);
  await expect(page.locator('.message.assistant strong')).toHaveText('Проверка детали');
  await expect(page.locator('.message.assistant li')).toHaveCount(2);
  await expect(page.locator('.message.assistant pre code')).toHaveText('result = 62\nprint(result)\n');
  await page.getByRole('button', { name: 'Копировать код', exact: true }).click();
  await expect.poll(() => page.evaluate(async () => (await navigator.clipboard.readText()).replace(/\r\n/g, '\n')))
    .toBe('result = 62\nprint(result)\n');
  await page.getByRole('button', { name: 'Копировать ответ', exact: true }).click();
  await expect.poll(() => page.evaluate(async () => (await navigator.clipboard.readText()).replace(/\r\n/g, '\n'))).toBe(response);
  await expect(page.getByRole('link', { name: 'Справка', exact: true })).toHaveAttribute('rel', /noreferrer/);
  expect(await page.locator('.message.assistant a[href^="javascript:"]').count()).toBe(0);
  await expect(page.locator('.message.assistant img')).toHaveCount(0);
  expect(requests).toEqual([]);
});

test('Enter отправляет один запрос, Shift+Enter сохраняет новую строку', async ({ page }) => {
  await prepare(page, false);
  await page.getByRole('button', { name: 'Новый диалог', exact: true }).click();
  let sent = 0;
  page.on('request', request => { if (request.method() === 'POST' && request.url().endsWith('/api/runs')) sent++; });
  const input = page.getByLabel('Сообщение', { exact: true });
  await input.fill('Прочитай');
  await input.press('Shift+Enter');
  await input.pressSequentially('размер');
  await expect(input).toHaveValue('Прочитай\nразмер');
  expect(sent).toBe(0);
  await input.press('Enter');
  await expect(page.getByText('Размер 62 мм. Готово.', { exact: true })).toBeVisible();
  await expect(input).toHaveValue('');
  expect(sent).toBe(1);
});

test('настройки возвращают фокус после Escape и кнопки закрытия', async ({ page }) => {
  await prepare(page);
  const opener = page.getByRole('button', { name: 'Настройки', exact: true });
  const dialog = page.getByRole('dialog', { name: 'Настройки', exact: true });
  await opener.click();
  await expect(dialog).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  await expect(opener).toBeFocused();
  await opener.click();
  await page.getByRole('button', { name: 'Закрыть настройки' }).click();
  await expect(dialog).toHaveCount(0);
  await expect(opener).toBeFocused();
});

test('выбор модели в шапке сохраняется, копирование находится в строке автора', async ({ page }) => {
  await prepare(page);
  const selector = page.getByRole('banner').getByLabel('Активное подключение');
  await expect(selector).toBeVisible();
  const other = await page.request.post('/api/profiles', { data: {
    name: 'Другая модель', base_url: 'http://localhost/v1', model: 'another-model', local: true, vision: true,
  } });
  const profile = await other.json();
  await page.reload();
  await selector.selectOption(profile.id);
  await page.reload();
  await expect(selector).toHaveValue(profile.id);
  const author = (await page.locator('.message.assistant .message-author').boundingBox())!;
  const copy = (await page.getByRole('button', { name: 'Копировать ответ', exact: true }).boundingBox())!;
  expect(Math.abs(author.y + author.height / 2 - copy.y - copy.height / 2)).toBeLessThan(5);
  await expect(page.getByText('Принимает изображения', { exact: false })).toHaveCount(0);
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  await expect(page.getByText('Снимки SolidWorks передаются модели при включённой поддержке изображений.', { exact: false })).toBeVisible();
});

test('журнал показывает последние операции в прокручиваемой области и сохраняет старые ошибки', async ({ page }) => {
  const events = Array.from({ length: 12 }, (_, index) => [
    { seq: index * 2 + 3, type: 'tool_start', data: { tool_call_id: `call-${index}`,
      name: 'get_dimension', arguments: { name: `D${index + 1}` } } },
    { seq: index * 2 + 4, type: 'tool_result', data: { tool_call_id: `call-${index}`,
      result: { ok: index !== 2, error: index === 2 ? 'Тестовая ошибка' : null } } },
  ]).flat();
  await prepare(page, true, events);
  await page.getByRole('button', { name: 'Журнал операций' }).click();
  const list = page.getByRole('region', { name: 'Операции CAD' });
  await expect(list).toBeVisible();
  const rows = list.locator(':scope > details');
  await expect(rows).toHaveCount(12);
  expect(await list.evaluate(el => el.clientHeight)).toBeLessThanOrEqual(240);
  expect(await list.evaluate(el => el.scrollHeight)).toBeGreaterThan(400);
  await expect(rows.last()).toBeInViewport();
  await expect(rows.first()).not.toBeInViewport();
  await list.evaluate(el => { el.scrollTop = 0; });
  await expect(rows.first()).toBeInViewport();
  await rows.nth(2).locator('summary').click();
  await expect(rows.nth(2).getByText('Ошибка', { exact: true })).toBeVisible();
  await expect(rows.nth(2).locator('pre')).toContainText('Тестовая ошибка');
});
