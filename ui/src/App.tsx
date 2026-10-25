import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { api, remember, stored, terminal } from './api';
import type { Confirmation, Profile, Run, RunEvent } from './api';
import ModelPicker from './ModelPicker';
import './styles.css';

const statusNames: Record<string, string> = {
  queued: 'В очереди', running: 'Выполняется', awaiting_confirmation: 'Ожидает подтверждения',
  stopping: 'Остановка', completed: 'Завершён', stopped: 'Остановлен', failed: 'Ошибка',
  uncertain: 'Исход неизвестен', limit_reached: 'Достигнут лимит', rejected: 'Отклонён',
};
const toolNames: Record<string, string> = {
  get_dimension: 'Чтение размера', set_dimension: 'Изменение размера', screenshot: 'Снимок',
  get_feature_tree: 'Чтение дерева', execute_python: 'Python-код', save_document: 'Сохранение',
};
const errorText = (error: unknown) => error instanceof Error ? error.message : 'Неизвестная ошибка.';
const localAddress = (value: string) => {
  try {
    const host = new URL(value).hostname;
    const octets = host.split('.');
    return host === 'localhost' || host === '[::1]' || (octets.length === 4 && octets[0] === '127'
      && octets.every(part => /^\d{1,3}$/.test(part) && Number(part) <= 255));
  }
  catch { return false; }
};

function Connections({ profiles, selected, onSaved, onClose, maxCalls, onMaxCallsChange }: {
  profiles: Profile[]; selected: string; onSaved: (id: string) => Promise<void>; onClose: () => void;
  maxCalls: number; onMaxCallsChange: (value: number) => void;
}) {
  const blank = { name: '', base_url: 'http://127.0.0.1:8080/v1', model: '', vision: false, local: true,
    reasoning_effort: null as string | null };
  const [editing, setEditing] = useState(selected);
  const [form, setForm] = useState(() => profiles.find(p => p.id === selected) ?? blank);
  const [key, setKey] = useState('');
  const [clearKey, setClearKey] = useState(false);
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const closeButton = useRef<HTMLButtonElement>(null);
  useEffect(() => { closeButton.current?.focus(); }, []);

  function edit(id: string) {
    setEditing(id); setForm(profiles.find(p => p.id === id) ?? blank);
    setKey(''); setClearKey(false); setError(''); setNotice('');
  }
  async function save(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError(''); setNotice('');
    try {
      const body = { name: form.name, base_url: form.base_url, model: form.model,
        local: form.local, vision: form.vision, reasoning_effort: form.reasoning_effort,
        ...(key || clearKey ? { api_key: clearKey ? '' : key } : {}) };
      const result = await api<Profile>(editing ? `/profiles/${editing}` : '/profiles', {
        method: editing ? 'PUT' : 'POST', body: JSON.stringify(body),
      });
      setEditing(result.id); setForm(result); setKey(''); setClearKey(false);
      await onSaved(result.id); setNotice('Подключение сохранено');
    } catch (error) { setError(errorText(error)); } finally { setBusy(false); }
  }
  async function probe() {
    setBusy(true); setError(''); setNotice('');
    try {
      const result = await api<{ message: string }>(`/profiles/${editing}/test`, { method: 'POST' });
      setNotice(result.message);
    } catch (error) { setError(errorText(error)); } finally { setBusy(false); }
  }
  async function remove() {
    setBusy(true); setError('');
    try { await api(`/profiles/${editing}`, { method: 'DELETE' }); await onSaved(''); edit(''); }
    catch (error) { setError(errorText(error)); } finally { setBusy(false); }
  }
  return <section id="app-settings" className="settings panel" aria-label="Настройки">
    <div className="section-title"><h2>Настройки</h2>
      <button ref={closeButton} onClick={onClose}>Закрыть настройки</button></div>
    <p className="local-badge">На этом компьютере</p>
    <h3>Подключения</h3>
    <p className="muted">OpenAI-совместимый API с вызовами инструментов. Изменения действуют со следующего запроса.</p>
    <div className="settings-layout">
      <nav aria-label="Сохранённые подключения">
        {profiles.map(profile => <button key={profile.id} className={editing === profile.id ? 'selected' : ''}
          onClick={() => edit(profile.id)}>{profile.name}<small>{profile.model}</small></button>)}
        <button onClick={() => edit('')}>Новое подключение</button>
      </nav>
      <form onSubmit={save}>
        <label>Название подключения<input required maxLength={200} value={form.name}
          onChange={e => setForm({ ...form, name: e.target.value })} /></label>
        <label>Адрес API<input required type="url" value={form.base_url} placeholder="https://api.example.com/v1"
          onChange={e => setForm({ ...form, base_url: e.target.value, local: localAddress(e.target.value) })} /></label>
        <ModelPicker baseUrl={form.base_url} profileId={editing} value={form.model} disabled={busy}
          apiKey={clearKey ? '' : key || undefined} onChange={model => setForm({ ...form, model })} />
        <label>Сила рассуждения<select value={form.reasoning_effort ?? ''} disabled={busy}
          aria-describedby="reasoning-hint"
          onChange={e => setForm({ ...form, reasoning_effort: e.target.value || null })}>
          <option value="">По умолчанию API</option>
          <option value="none">Без рассуждения</option>
          <option value="minimal">Минимальная</option>
          <option value="low">Низкая</option>
          <option value="medium">Средняя</option>
          <option value="high">Высокая</option>
          <option value="xhigh">Очень высокая</option>
          <option value="max">Максимальная</option>
        </select></label>
        <p id="reasoning-hint" className="hint">Доступные уровни зависят от модели и API. Выбор сохраняется кнопкой «Сохранить подключение».</p>
        <label>Ключ API<input type="password" autoComplete="new-password" value={key}
          placeholder={profiles.find(p => p.id === editing)?.has_key ? 'Ключ сохранён. Оставьте пустым, чтобы сохранить его.' : 'Для локального API необязателен'}
          onChange={e => { setKey(e.target.value); setClearKey(false); }} /></label>
        {profiles.find(p => p.id === editing)?.has_key ? <label className="check"><input type="checkbox"
          checked={clearKey} onChange={e => setClearKey(e.target.checked)} />Удалить сохранённый ключ</label> : null}
        <label className="check"><input type="checkbox" checked={form.vision}
          onChange={e => setForm({ ...form, vision: e.target.checked })} />Поддержка изображений</label>
        <label className="check"><input type="checkbox" checked={form.local}
          onChange={e => setForm({ ...form, local: e.target.checked })} />Локальное подключение</label>
        <p className="hint">Данные отправляются выбранному API. Изображения передаются при включённой поддержке изображений. Отмечайте сервер локальным, только если доверяете ему.</p>
        {notice ? <p role="status" className="notice">{notice}</p> : null}
        {error ? <p role="alert" className="error">{error}</p> : null}
        <div className="actions"><button className="primary" disabled={busy} type="submit">Сохранить подключение</button>
          <button disabled={busy || !editing} type="button" onClick={probe}>Проверить подключение</button>
          <button className="danger" disabled={busy || !editing} type="button" onClick={remove}>Удалить подключение</button>
        </div>
      </form>
    </div>
    <div className="run-settings">
      <h3>Параметры запросов</h3>
      <label>Лимит вызовов<input type="number" min={1} max={1000} value={maxCalls}
        onChange={e => onMaxCallsChange(Number(e.target.value))} /></label>
    </div>
  </section>;
}

function ConfirmationPanel({ confirmation, onAnswer }: {
  confirmation: Confirmation; onAnswer: (approved: boolean) => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const firstButton = useRef<HTMLButtonElement>(null);
  useEffect(() => { firstButton.current?.focus(); }, []);
  async function answer(approved: boolean) {
    setBusy(true);
    try { await onAnswer(approved); } finally { setBusy(false); }
  }
  return <section role="dialog" aria-label="Подтверждение Python-кода"
    className="confirmation panel">
    <div className="eyebrow">Нужно ваше решение</div>
    <h2>Выполнение Python-кода</h2>
    <p>Разрешить выполнение Python-кода в этом диалоге для выбранного подключения? Следующие фрагменты будут выполняться без повторного вопроса. Код имеет доступ к SolidWorks и системе.</p>
    <pre tabIndex={0}>{confirmation.code}</pre>
    <div className="actions"><button ref={firstButton} className="primary" disabled={busy}
      onClick={() => answer(true)}>Разрешить в этом диалоге</button>
      <button disabled={busy} onClick={() => answer(false)}>Отклонить</button></div>
  </section>;
}

function OperationLog({ events }: { events: RunEvent[] }) {
  const starts = events.filter(e => e.type === 'tool_start');
  return <details className="operations"><summary role="button">Журнал операций</summary>
    {starts.length === 0 ? <p className="muted">Инструменты ещё не вызывались.</p> : starts.map(start => {
      const id = start.data.tool_call_id;
      const result = events.filter(e => e.type === 'tool_result' && e.data.tool_call_id === id).at(-1);
      const value = result?.data.result as { ok?: boolean; uncertain?: boolean; verification?: { status: string } } | undefined;
      const name = String(start.data.name);
      return <details key={start.seq}><summary><span>{toolNames[name] ?? 'CAD-операция'} · <code>{name}</code></span>
        <small>{start.data.verification ? 'Проверка · ' : ''}{result ? (value?.uncertain ? 'Исход неизвестен' : value?.ok ? 'Выполнено' : 'Ошибка') : 'Ожидание'}</small></summary>
        <pre tabIndex={0}>{JSON.stringify({ arguments: start.data.arguments, result: result?.data.result }, null, 2)}</pre>
      </details>;
    })}
  </details>;
}

function RunView({ run }: { run: Run }) {
  return <article className="run" aria-label="Запрос">
    {run.events.filter(event => ['message', 'image', 'error'].includes(event.type)).map(event => {
      if (event.type === 'message') return <div key={event.seq}
        className={`message ${event.data.role === 'user' ? 'user' : 'assistant'}`}>
        <span className="message-author">{event.data.role === 'user' ? 'Вы' : 'CAD-агент'}</span>
        <p>{String(event.data.content)}</p></div>;
      if (event.type === 'image') return <figure key={event.seq}><a target="_blank" rel="noreferrer"
        href={`data:${event.data.mimeType};base64,${event.data.data}`} download="solidworks.png">
        <img src={`data:${event.data.mimeType};base64,${event.data.data}`} alt="Снимок SolidWorks" /></a>
        <figcaption>Снимок из SolidWorks</figcaption></figure>;
      return <p key={event.seq} className="error">{String(event.data.message)}</p>;
    })}
    <div className="run-meta"><span className={`badge ${run.status}`} data-testid="run-status">{statusNames[run.status] ?? run.status}</span>
      <span>Вызовов: {run.call_count}</span>
      {run.verification === 'unverified' ? <strong className="warning">Результат требует проверки</strong> : null}
      {run.verification === 'verified' ? <span>Проверки пройдены</span> : null}
      {run.verification === 'failed' ? <strong className="error">Проверка не пройдена</strong> : null}
    </div>
    {run.status === 'stopping' ? <p className="hint">Ждём исход начатой операции. Следующих шагов не будет; выполненное не откатывается.</p> : null}
    {run.status === 'uncertain' ? <p className="error">Проверьте SolidWorks и перезапустите приложение. Автоматического повтора не будет.</p> : null}
    <OperationLog events={run.events} />
  </article>;
}

export default function App() {
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [selected, setSelected] = useState(() => stored('profile', ''));
  const [sessionId, setSessionId] = useState(() => stored('session', crypto.randomUUID()));
  const [runs, setRuns] = useState<Run[]>([]);
  const [streamId, setStreamId] = useState('');
  const [settings, setSettings] = useState(false);
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [maxCalls, setMaxCalls] = useState(80);
  const end = useRef<HTMLDivElement>(null);
  const pendingRequest = useRef('');
  const active = runs.find(run => !terminal(run.status));
  const uncertain = runs.some(run => run.status === 'uncertain');
  const currentProfile = profiles.find(profile => profile.id === selected);

  function merge(snapshot: Run) {
    setRuns(previous => {
      const current = previous.find(run => run.id === snapshot.id);
      if (current && current.events.length > snapshot.events.length) return previous;
      return current ? previous.map(run => run.id === snapshot.id ? snapshot : run) : [...previous, snapshot];
    });
  }
  useEffect(() => {
    let disposed = false;
    remember('session', sessionId);
    Promise.all([api<Profile[]>('/profiles'), api<{ active_run_id: string | null }>('/health'),
      api<Run[]>(`/runs?session_id=${encodeURIComponent(sessionId)}`)])
      .then(async ([profiles, health, history]) => {
        if (health.active_run_id && !history.some(run => run.id === health.active_run_id)) {
          const run = await api<Run>(`/runs/${health.active_run_id}`);
          history.push(run);
          if (!disposed) { setSessionId(run.session_id); remember('session', run.session_id); }
        }
        if (disposed) return;
        setProfiles(profiles); setRuns(history);
        setSelected(previous => profiles.some(p => p.id === previous) ? previous : profiles[0]?.id ?? '');
        const unfinished = health.active_run_id || history.find(run => !terminal(run.status))?.id;
        if (unfinished) setStreamId(unfinished);
      }).catch(error => { if (!disposed) setError(errorText(error)); })
      .finally(() => { if (!disposed) setLoading(false); });
    return () => { disposed = true; };
    // Initial restore only: a new dialogue is an explicit action below.
  }, []);

  useEffect(() => {
    const leave = () => {
      if (pendingRequest.current) navigator.sendBeacon(`/api/requests/${pendingRequest.current}/stop`);
    };
    window.addEventListener('pagehide', leave);
    return () => window.removeEventListener('pagehide', leave);
  }, []);

  useEffect(() => {
    if (!streamId) return;
    let disposed = false;
    const source = new EventSource(`/api/runs/${streamId}/events`);
    async function refresh() {
      try {
        const snapshot = await api<Run>(`/runs/${streamId}`);
        if (disposed) return;
        merge(snapshot);
        if (terminal(snapshot.status)) source.close();
      } catch (error) { if (!disposed) setError(errorText(error)); }
    }
    source.addEventListener('update', refresh);
    source.onerror = () => { void refresh(); };
    const poll = window.setInterval(refresh, 1000);
    const leave = () => { navigator.sendBeacon(`/api/runs/${streamId}/stop`); };
    window.addEventListener('pagehide', leave);
    return () => { disposed = true; source.close(); clearInterval(poll); window.removeEventListener('pagehide', leave); };
  }, [streamId]);

  useEffect(() => { end.current?.scrollIntoView({ block: 'nearest' }); }, [runs.at(-1)?.events.length]);

  async function reloadProfiles(id: string) {
    const profiles = await api<Profile[]>('/profiles'); setProfiles(profiles);
    const selected = id || profiles[0]?.id || ''; setSelected(selected); remember('profile', selected);
  }
  async function send(event: FormEvent) {
    event.preventDefault(); if (sending || active || !selected || !message.trim()) return;
    setSending(true); setError('');
    pendingRequest.current = crypto.randomUUID();
    try {
      const run = await api<Run>('/runs', { method: 'POST', body: JSON.stringify({
        session_id: sessionId, profile_id: selected, message, max_calls: maxCalls,
        request_id: pendingRequest.current,
      }) });
      merge(run); setStreamId(run.id); setMessage(''); remember('session', sessionId);
    } catch (error) { setError(errorText(error)); } finally { setSending(false); }
  }
  async function stop() {
    if (!active) return;
    try { merge(await api<Run>(`/runs/${active.id}/stop`, { method: 'POST' })); }
    catch (error) { setError(errorText(error)); }
  }
  async function confirm(approved: boolean) {
    if (!active?.confirmation) return;
    try { merge(await api<Run>(`/runs/${active.id}/confirm`, { method: 'POST', body: JSON.stringify({
      confirmation_id: active.confirmation.id, approved,
    }) })); } catch (error) { setError(errorText(error)); }
  }
  function newDialogue() {
    const id = crypto.randomUUID(); setSessionId(id); remember('session', id);
    setStreamId(''); setRuns([]); setError('');
  }

  return <div className="shell">
    <header className="app-header"><div className="brand"><div className="brand-icon" aria-hidden="true">C</div>
      <div><h1>CAD-Agent-Next</h1><p>Рабочий чат SolidWorks</p></div></div>
      <div className="header-actions">
        <button aria-expanded={settings} aria-controls="app-settings"
          onClick={() => setSettings(value => !value)}>Настройки</button></div></header>
    <main>
      <div className="toolbar"><label>Активное подключение<select value={selected}
        onChange={e => { setSelected(e.target.value); remember('profile', e.target.value); }} disabled={loading}>
        <option value="">Выберите подключение</option>{profiles.map(profile => <option key={profile.id}
          value={profile.id}>{profile.name} · {profile.model}</option>)}</select></label>
        <button disabled={!!active || sending} onClick={newDialogue}>Новый диалог</button></div>
      {currentProfile ? <p className="connection-note">{currentProfile.local ? 'Локальная модель' : 'Внешний провайдер'}
        {' · '}{currentProfile.vision ? 'Принимает изображения' : 'Текстовая модель'}
        {active ? ' · Текущий запрос сохраняет своё подключение' : ''}</p> : null}
      {settings ? <Connections profiles={profiles} selected={selected} onSaved={reloadProfiles}
        maxCalls={maxCalls} onMaxCallsChange={setMaxCalls} onClose={() => setSettings(false)} /> : null}
      {error ? <p role="alert" className="error panel">{error}</p> : null}
      <section className="chat panel" aria-label="Чат">
        {loading ? <p role="status">Загружаем состояние приложения…</p> : null}
        {!loading && runs.length === 0 ? <div className="empty"><div className="eyebrow">От запроса к модели</div>
          <h2>Начнём с вашей детали</h2><p>Прочитайте размер, изучите дерево или попросите показать снимок из SolidWorks.</p>
          {!profiles.length ? <button className="primary" onClick={() => setSettings(true)}>Добавить подключение</button> : null}
          <p className="hint">SolidWorks должен быть запущен. Действия и их проверки появятся в журнале.</p></div> : null}
        {runs.map(run => <RunView key={run.id} run={run} />)}
        {active?.confirmation ? <ConfirmationPanel key={active.confirmation.id}
          confirmation={active.confirmation} onAnswer={confirm} /> : null}
        <div ref={end} />
      </section>
      <form className="composer panel" onSubmit={send}>
        <label className="composer-label">Сообщение<textarea rows={3} value={message} maxLength={20000}
          onChange={e => setMessage(e.target.value)} placeholder="Например: прочитай D1@Эскиз1 и покажи снимок"
          disabled={loading} /></label>
        <div className="composer-footer">
          <div className="actions">{active ? <button type="button" className="danger" onClick={stop}
            disabled={active.status === 'stopping'}>Остановить</button> : null}
            <button className="primary" type="submit" disabled={loading || sending || !!active || !selected || !message.trim() || uncertain}>
              {sending ? 'Отправляем…' : 'Отправить'}</button></div></div>
      </form>
    </main>
  </div>;
}
