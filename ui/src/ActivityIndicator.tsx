import { useEffect, useState } from 'react';
import { terminal } from './api';
import type { Run, RunEvent } from './api';

export interface PendingActivity {
  phase: 'images' | 'sending'; at: number; files: number; bytes: number;
}
export interface RunContact {
  runId: string; at: number; failed: boolean;
}

const finishedNames: Record<string, string> = {
  completed: 'Запрос завершён', stopped: 'Запрос остановлен', failed: 'Запрос завершился ошибкой',
  uncertain: 'Результат операции неизвестен', limit_reached: 'Достигнут лимит вызовов', rejected: 'Действие отклонено',
};
const toolNames: Record<string, string> = {
  connect_solidworks: 'подключение', list_open_documents: 'список документов', create_part: 'создание детали',
  create_assembly: 'создание сборки', create_sketch: 'создание эскиза', close_sketch: 'закрытие эскиза',
  sketch_line: 'построение линии', sketch_circle: 'построение окружности', sketch_rectangle: 'построение прямоугольника',
  create_base_flange: 'создание базового фланца', cut_through: 'создание выреза', rebuild: 'перестроение',
  set_dimension: 'изменение размера', get_dimension: 'чтение размера', get_feature_tree: 'чтение дерева',
  get_mass_properties: 'чтение свойств', get_sheet_metal_info: 'параметры листового металла',
  screenshot: 'снимок детали', execute_python: 'выполнение Python-кода', save_document: 'сохранение',
  export_flat_pattern_dxf: 'экспорт развёртки', get_components: 'список компонентов',
  add_component: 'добавление компонента', add_mate: 'добавление сопряжения',
};

function stamp(event?: RunEvent) {
  return event?.at !== undefined && Number.isFinite(event.at) ? event.at * 1000 : undefined;
}
function duration(start: number | undefined, end: number) {
  if (start === undefined) return '—';
  const seconds = Math.max(0, Math.floor((end - start) / 1000));
  const parts = [Math.floor(seconds / 60) % 60, seconds % 60];
  if (seconds >= 3600) parts.unshift(Math.floor(seconds / 3600));
  return parts.map(value => String(value).padStart(2, '0')).join(':');
}
function stageOf(run: Run) {
  const latestFirst = [...run.events].reverse();
  const status = latestFirst.find(event => event.type === 'status' && event.data.status === run.status);
  if (terminal(run.status)) return { label: finishedNames[run.status], at: stamp(status), kind: 'finished' };
  if (run.status === 'stopping') return { label: 'Останавливаем запрос', at: stamp(status), kind: 'stopping' };
  if (run.status === 'awaiting_confirmation') {
    return { label: 'Ожидаем подтверждение Python-кода', at: stamp(status), kind: 'confirmation' };
  }
  if (run.status === 'repairing') {
    return { label: `Анализируем ошибку Python; попытка ${run.repair_attempts ?? 1} из 2`,
      at: stamp(status), kind: 'repair' };
  }
  const latest = latestFirst.find(event => ['model_start', 'model_end', 'tool_start', 'tool_result', 'status'].includes(event.type));
  if (latest?.type === 'model_start') return { label: 'Ожидаем ответ модели', at: stamp(latest), kind: 'model' };
  if (latest?.type === 'model_end') return { label: 'Обрабатываем ответ модели', at: stamp(latest), kind: 'response' };
  if (latest?.type === 'tool_start') {
    const name = String(latest.data.name);
    return { label: `${latest.data.verification ? 'Проверяем результат' : 'SolidWorks'}: ${toolNames[name] ?? name}`,
      at: stamp(latest), kind: 'cad' };
  }
  return { label: latest?.type === 'tool_result' ? 'Обрабатываем результат SolidWorks' : 'Подготавливаем запрос',
    at: stamp(latest ?? run.events[0]), kind: 'preparing' };
}

export default function ActivityIndicator({ run, pending, contact }: {
  run?: Run; pending: PendingActivity | null; contact: RunContact | null;
}) {
  const [now, setNow] = useState(Date.now);
  const active = !!pending || (!!run && !terminal(run.status));
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  if (!run && !pending) return null;

  const stage = pending
    ? { label: pending.phase === 'images' ? 'Подготавливаем изображения' : 'Отправляем запрос', at: pending.at, kind: pending.phase }
    : stageOf(run!);
  const lastEvent = run?.events.at(-1);
  const firstAt = pending?.at ?? stamp(run?.events[0]);
  const endedAt = !pending && run && terminal(run.status) ? stage.at : undefined;
  const end = endedAt ?? now;
  const observed = !pending && run && contact?.runId === run.id ? contact : null;
  const disconnected = active && !pending && (!observed || observed.failed || now - observed.at >= 8000);
  const longWait = stage.kind === 'model' && stage.at !== undefined && now - stage.at >= 30000;
  const label = disconnected ? 'Связь с приложением потеряна' : longWait ? 'Ответ модели ещё не получен' : stage.label;
  const pulse = active && !disconnected && stage.kind !== 'confirmation';
  const modelStarts = run?.events.filter(event => event.type === 'model_start').length ?? 0;
  const modelReplies = run?.events.filter(event => event.type === 'model_end' && event.data.outcome === 'received').length ?? 0;
  const cadStarts = new Set(run?.events.filter(event => event.type === 'tool_start').map(event => event.data.tool_call_id));
  const cadResults = new Set(run?.events.filter(event => event.type === 'tool_result' && cadStarts.has(event.data.tool_call_id))
    .map(event => event.data.tool_call_id));
  const elapsed = endedAt !== undefined ? duration(firstAt, endedAt) : duration(stage.at, now);

  return <section className={`activity-indicator${disconnected ? ' disconnected' : ''}`}
    role="region" aria-label="Ход выполнения" data-running={pulse}>
    <details key={pending ? `pending-${pending.at}` : run?.id}>
      <summary role="button" aria-label={`${label}. Подробности выполнения`}>
        <span className="activity-dot" aria-hidden="true" />
        <span className="activity-stage" role="status" aria-live="polite" aria-atomic="true">{label}</span>
        <span className="activity-time" aria-label="Время текущего этапа" aria-live="off">{elapsed}</span>
        <span className="activity-chevron" aria-hidden="true">⌄</span>
      </summary>
      <div className="activity-details">
        {disconnected ? <p className="warning">Не удаётся обновить состояние. Последний известный этап: {stage.label}.
          {' '}Запрос повторно не отправляется.</p> : null}
        {stage.kind === 'stopping' ? <p>Ждём завершения текущего обращения. Следующих действий не будет.</p> : null}
        <p>Общее время: {duration(firstAt, end)}{firstAt === undefined ? ' (время не записано)' : ''}</p>
        {pending ? <p>Изображений: {pending.files} · Исходные файлы: {new Intl.NumberFormat('ru-RU', {
          maximumFractionDigits: 1 }).format(pending.bytes / 1024)} КБ</p> : <>
          <p>Обращений к модели: {modelStarts} · Ответов получено: {modelReplies}</p>
          <p>CAD-операций завершено: {cadResults.size} из {cadStarts.size} (включая проверки)</p>
          {active ? <p>Последнее событие: {duration(stamp(lastEvent), now)} назад.
            {!disconnected ? ' Приложение отвечает.' : ''}</p> : null}
        </>}
      </div>
    </details>
  </section>;
}
