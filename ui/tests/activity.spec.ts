import { expect, test, type Page } from '@playwright/test';
import type { Run, RunEvent } from '../src/api';

function event(seq: number, type: string, data: Record<string, unknown>, at = Date.now() / 1000): RunEvent & { at: number } {
  return { seq, type, data, at };
}

async function prepare(page: Page, changes: Partial<Run> = {}) {
  const profile = await (await page.request.post('/api/profiles', { data: {
    name: 'Активность', base_url: 'http://localhost/v1', model: 'fake-model',
  } })).json();
  let run: Run = {
    id: 'activity-run', session_id: 'activity-session', profile_id: profile.id, status: 'running',
    confirmation: null, call_count: 0, max_calls: 80, verification: 'not_required', stop_requested: false,
    events: [event(1, 'message', { role: 'user', content: 'Подумай' }, Date.now() / 1000 - 22),
      event(2, 'model_start', { number: 1 }, Date.now() / 1000 - 20)], ...changes,
  };
  let offline = false;
  let sent = run.events.at(-1)?.seq ?? 0;
  await page.addInitScript(id => {
    localStorage.setItem('cadnext.v1.profile', id);
    localStorage.setItem('cadnext.v1.session', 'activity-session');
  }, profile.id);
  await page.route('**/api/health', route => route.fulfill({ json: { ok: true,
    active_run_id: ['completed', 'failed', 'stopped'].includes(run.status) ? null : run.id, uncertain: false } }));
  await page.route('**/api/runs?session_id=activity-session', route => route.fulfill({ json: [run] }));
  await page.route('**/api/runs/activity-run', route => offline ? route.abort() : route.fulfill({ json: run }));
  await page.addInitScript(() => {
    const target = window as typeof window & { activitySources: EventTarget[]; activityOffline: boolean };
    target.activitySources = [];
    target.activityOffline = false;
    class ActivitySource extends EventTarget {
      onerror: (() => void) | null = null;
      onopen: (() => void) | null = null;
      timer: number;
      constructor() {
        super();
        target.activitySources.push(this);
        setTimeout(() => this.onopen?.(), 0);
        this.timer = window.setInterval(() => {
          if (target.activityOffline) this.onerror?.();
          else this.dispatchEvent(new MessageEvent('heartbeat', { data: '{}' }));
        }, 500);
      }
      close() { clearInterval(this.timer); }
    }
    Object.defineProperty(window, 'EventSource', { value: ActivitySource });
  });
  await page.goto('/');
  return {
    profileId: profile.id as string,
    indicator: page.getByRole('region', { name: 'Ход выполнения', exact: true }),
    update: async (changes: Partial<Run>) => {
      run = { ...run, ...changes };
      let updates = run.events.filter(item => item.seq > sent);
      if (!updates.length) {
        const change = event(sent + 1, 'status', { status: run.status });
        run = { ...run, events: [...run.events, change] };
        updates = [change];
      }
      sent = updates.at(-1)!.seq;
      const { events: _events, ...state } = run;
      await page.evaluate(({ updates, state }) => {
        const target = window as typeof window & { activitySources: EventTarget[] };
        for (const source of target.activitySources) for (const update of updates) {
          source.dispatchEvent(new MessageEvent('update', { data: JSON.stringify({ ...update, state }) }));
        }
      }, { updates, state });
    },
    offline: async (value: boolean) => {
      offline = value;
      await page.evaluate(value => {
        (window as typeof window & { activityOffline: boolean }).activityOffline = value;
      }, value);
    },
  };
}

test('ожидание модели показывает живой таймер и сохраняет время после обновления', async ({ page }) => {
  const { indicator } = await prepare(page);
  await expect(indicator.getByRole('status')).toHaveText('Ожидаем ответ модели');
  await expect(indicator.getByRole('button', { name: 'Подробности выполнения' }))
    .toHaveAccessibleName('Ожидаем ответ модели. Подробности выполнения');
  const clock = indicator.getByLabel('Время текущего этапа');
  await expect(clock).toHaveText(/^00:2\d$/);
  const initial = await clock.textContent();
  await expect.poll(() => clock.textContent()).not.toBe(initial);
  await indicator.getByRole('button', { name: 'Подробности выполнения' }).click();
  await expect(indicator).toContainText('Обращений к модели: 1');
  await expect(indicator).toContainText('Ответов получено: 0');
  await page.reload();
  await expect(clock).toHaveText(/^00:2\d$/);
  for (const width of [1280, 390]) {
    await page.setViewportSize({ width, height: 844 });
    await expect(indicator).toBeInViewport();
    await expect(page.getByLabel('Сообщение', { exact: true })).toBeInViewport();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
});

test('долгое ожидание сообщает об отсутствии ответа, потеря связи снимает пульсацию и восстанавливается', async ({ page }) => {
  const { indicator, offline } = await prepare(page, { events: [
    event(1, 'message', { role: 'user', content: 'Жди' }, Date.now() / 1000 - 46),
    event(2, 'model_start', { number: 1 }, Date.now() / 1000 - 45),
  ] });
  await expect(indicator.getByRole('status')).toHaveText('Ответ модели ещё не получен');
  await expect(indicator).toHaveAttribute('data-running', 'true');
  offline(true);
  await expect(indicator.getByRole('status')).toHaveText('Связь с приложением потеряна');
  await expect(indicator).toHaveAttribute('data-running', 'false');
  offline(false);
  await expect(indicator.getByRole('status')).toHaveText('Ответ модели ещё не получен');
  await expect(indicator).toHaveAttribute('data-running', 'true');
});

test('этапы CAD и проверки используют реальные события, повторный результат не удваивает счётчик', async ({ page }) => {
  const events = [
    event(1, 'message', { role: 'user', content: 'Измени' }),
    event(2, 'model_start', { number: 1 }),
    event(3, 'model_end', { number: 1, outcome: 'received' }),
    event(4, 'tool_start', { tool_call_id: 'change', name: 'set_dimension', verification: false }),
  ];
  const { indicator, update } = await prepare(page, { events, call_count: 1 });
  await expect(indicator.getByRole('status')).toHaveText('SolidWorks: изменение размера');
  events.push(event(5, 'tool_result', { tool_call_id: 'change', result: { ok: true } }));
  events.push(event(6, 'tool_start', { tool_call_id: 'check', name: 'get_dimension', verification: true }));
  update({ events: [...events], call_count: 2 });
  await expect(indicator.getByRole('status')).toHaveText('Проверяем результат: чтение размера');
  events.push(event(7, 'tool_result', { tool_call_id: 'check', result: { ok: true } }));
  events.push(event(8, 'tool_result', { tool_call_id: 'change', result: { ok: true, verification: { status: 'verified' } } }));
  events.push(event(9, 'model_start', { number: 2 }));
  update({ events: [...events] });
  await expect(indicator.getByRole('status')).toHaveText('Ожидаем ответ модели');
  await indicator.getByRole('button', { name: 'Подробности выполнения' }).click();
  await expect(indicator).toContainText('Обращений к модели: 2');
  await expect(indicator).toContainText('Ответов получено: 1');
  await expect(indicator).toContainText('CAD-операций завершено: 2 из 2');
});

for (const [status, text] of [
  ['completed', 'Запрос завершён'], ['failed', 'Запрос завершился ошибкой'], ['stopped', 'Запрос остановлен'],
] as const) {
  test(`индикатор завершает ожидание: ${status}`, async ({ page }) => {
    const events = [event(1, 'message', { role: 'user', content: 'Подумай' }, Date.now() / 1000 - 20),
      event(2, 'model_start', { number: 1 }, Date.now() / 1000 - 18)];
    const { indicator, update } = await prepare(page, { events });
    await expect(indicator).toHaveAttribute('data-running', 'true');
    const ended = Date.now() / 1000;
    events.push(event(3, 'model_end', { number: 1, outcome: status === 'failed' ? 'error' : 'received' }, ended));
    events.push(event(4, 'status', { status, message: text }, ended));
    update({ status, events: [...events] });
    await expect(indicator.getByRole('status')).toHaveText(text);
    await expect(indicator).toHaveAttribute('data-running', 'false');
    const clock = indicator.getByLabel('Время текущего этапа');
    const value = await clock.textContent();
    await page.clock.install();
    await page.clock.fastForward(10000);
    await expect(clock).toHaveText(value!);
  });
}

test('подтверждение и остановка обозначены отдельно от работы модели', async ({ page }) => {
  const { indicator, update } = await prepare(page, { status: 'awaiting_confirmation' });
  await expect(indicator.getByRole('status')).toHaveText('Ожидаем подтверждение Python-кода');
  await expect(indicator).toHaveAttribute('data-running', 'false');
  update({ status: 'stopping', stop_requested: true });
  await expect(indicator.getByRole('status')).toHaveText('Останавливаем запрос');
});

test('исправление Python выделено отдельным этапом и счётчиком попыток', async ({ page }) => {
  const { indicator } = await prepare(page, { status: 'repairing', repair_attempts: 1, events: [
    event(1, 'message', { role: 'user', content: 'Построй' }),
    event(2, 'model_start', { number: 1 }),
    event(3, 'model_end', { number: 1, outcome: 'received' }),
    event(4, 'repair', { attempt: 1 }),
    event(5, 'status', { status: 'repairing', message: 'Анализируем ошибку Python' }),
  ] });
  await expect(indicator.getByRole('status')).toHaveText('Анализируем ошибку Python; попытка 1 из 2');
  await expect(indicator).toHaveAttribute('data-running', 'true');
});

test('отправка вложения показывает этап и размер исходных файлов до ответа API', async ({ page }) => {
  const { indicator, profileId } = await prepare(page, { status: 'completed' });
  const updated = await page.request.put(`/api/profiles/${profileId}`, { data: { vision: true } });
  expect(updated.ok()).toBeTruthy();
  await page.reload();
  await expect(page.getByRole('button', { name: 'Прикрепить изображение', exact: true })).toBeEnabled();
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/api/runs', async route => {
    if (route.request().method() !== 'POST') return route.continue();
    await held;
    await route.fulfill({ status: 422, json: { detail: 'Тест завершён' } });
  });
  const buffer = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAACAAAAAYCAIAAAAUMWhjAAAANElEQVR4nGNUzrjDQEvARFPTGUYtIAKMxgFBMBpEBMFoEBEEo0FEEIwGEUEwGkQEAc2DCAChPAGXw19cBgAAAABJRU5ErkJggg==', 'base64');
  await page.getByLabel('Файлы изображений').setInputFiles({ name: 'деталь.png', mimeType: 'image/png', buffer });
  await page.getByRole('button', { name: 'Отправить', exact: true }).click();
  try {
    await expect(indicator.getByRole('status')).toHaveText('Отправляем запрос');
    await indicator.getByRole('button', { name: 'Подробности выполнения' }).click();
    await expect(indicator).toContainText('Изображений: 1');
    await expect(indicator).toContainText('Исходные файлы:');
  } finally { release(); }
});

test('настоящие события API показывают CAD-этап и итоговые счётчики', async ({ page }) => {
  const profile = await (await page.request.post('/api/profiles', { data: {
    name: 'Настоящие события', base_url: 'http://localhost/v1', model: 'fake-model',
  } })).json();
  await page.addInitScript(id => {
    localStorage.setItem('cadnext.v1.profile', id);
    localStorage.setItem('cadnext.v1.session', 'real-activity');
  }, profile.id);
  await page.goto('/');
  await page.getByLabel('Сообщение', { exact: true }).fill('Читай долго');
  await page.getByRole('button', { name: 'Отправить', exact: true }).click();
  const indicator = page.getByRole('region', { name: 'Ход выполнения', exact: true });
  await expect(indicator.getByRole('status')).toHaveText('SolidWorks: чтение размера');
  await indicator.getByRole('button', { name: 'Подробности выполнения' }).click();
  await expect(indicator.getByRole('status')).toHaveText('Запрос завершён');
  await expect(indicator).toContainText('Обращений к модели: 2');
  await expect(indicator).toContainText('Ответов получено: 2');
  await expect(indicator).toContainText('CAD-операций завершено: 2 из 2');
  await expect(indicator).toHaveAttribute('data-running', 'false');
});

test('reduced-motion отключает пульсацию, а старые события не получают выдуманное время', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  const { indicator } = await prepare(page, { events: [
    { seq: 1, type: 'message', data: { role: 'user', content: 'Старый запрос' } },
    { seq: 2, type: 'model_start', data: { number: 1 } },
  ] });
  await expect(indicator.getByLabel('Время текущего этапа')).toHaveText('—');
  await expect(indicator.locator('.activity-dot')).toHaveCSS('animation-name', 'none');
  await indicator.getByRole('button', { name: 'Подробности выполнения' }).click();
  await expect(indicator).toContainText('время не записано');
  await expect(indicator.getByLabel('Время текущего этапа')).toHaveAttribute('aria-live', 'off');
});
