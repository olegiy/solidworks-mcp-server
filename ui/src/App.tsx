import { useEffect, useRef, useState } from 'react';
import type { FormEvent, ReactNode, RefObject } from 'react';
import { api, remember, stored, terminal } from './api';
import type { Attachment, Confirmation, ModelCatalog, Profile, ReasoningMetadata, Run, RunEvent } from './api';
import { imageTypes, readImages, type DraftAttachment } from './attachments';
import ModelPicker from './ModelPicker';
import useReasoning from './useReasoning';
import MessageContent, { CopyButton } from './MessageContent';
import Icon from './Icon';
import ActivityIndicator from './ActivityIndicator';
import { applyRunEvent } from './runEvents';
import type { PendingActivity, RunContact } from './ActivityIndicator';
import { useI18n } from './i18n';
import type { MessageKey } from './i18n';
import { languages } from './language';
import type { Language } from './language';
import './styles.css';

const statusKeys: Record<string, MessageKey> = {
  queued: 'statusQueued', running: 'statusRunning', repairing: 'statusRepairing',
  awaiting_confirmation: 'statusAwaitingConfirmation', stopping: 'statusStopping',
  completed: 'statusCompleted', stopped: 'statusStopped', failed: 'statusFailed',
  uncertain: 'statusUncertain', limit_reached: 'statusLimitReached', rejected: 'statusRejected',
};
const toolKeys: Record<string, MessageKey> = {
  get_dimension: 'toolGetDimension', set_dimension: 'toolSetDimension', screenshot: 'toolScreenshot',
  get_feature_tree: 'toolGetFeatureTree', execute_python: 'toolExecutePython',
  save_document: 'toolSaveDocument',
};
type Translate = ReturnType<typeof useI18n>['t'];
const errorText = (error: unknown, t: Translate) =>
  error instanceof Error ? error.message : t('unknownError');
const fallbackReasoningOptions: readonly (readonly [string, MessageKey])[] = [
  ['', 'apiDefault'], ['none', 'reasoningNone'], ['minimal', 'reasoningMinimal'],
  ['low', 'reasoningLow'], ['medium', 'reasoningMedium'], ['high', 'reasoningHigh'],
  ['xhigh', 'reasoningXHigh'], ['max', 'reasoningMax'],
];
const reasoningLabels: Record<string, MessageKey> = Object.fromEntries(fallbackReasoningOptions
  .filter(([value]) => value).map(([value, key]) => [value, key]));
function reasoningOptionsFor(metadata: ReasoningMetadata | null | undefined, t: Translate) {
  const mandatory = metadata?.mandatory === true;
  const supported = metadata?.supported_efforts?.filter(value =>
    Object.prototype.hasOwnProperty.call(reasoningLabels, value) && (!mandatory || value !== 'none')) ?? [];
  if (!supported.length) {
    if (mandatory) return [['', t('apiDefault')]] as const;
    return fallbackReasoningOptions.map(([value, key]) => [value, t(key)] as const);
  }
  const options = mandatory ? [] : [['', t('apiDefault')] as const];
  return [...options, ...supported.map(value => [value,
    t(reasoningLabels[value] ?? 'apiDefault')] as const)];
}
function selectedReasoning(metadata: ReasoningMetadata | null | undefined, value: string | null | undefined, t: Translate) {
  const options = reasoningOptionsFor(metadata, t);
  if (options.some(([option]) => option === value)) return value ?? '';
  if (metadata?.mandatory) {
    if (options.some(([option]) => option === metadata.default_effort)) return metadata.default_effort ?? '';
    return options[0]?.[0] ?? '';
  }
  return '';
}
const localAddress = (value: string) => {
  try {
    const host = new URL(value).hostname;
    const octets = host.split('.');
    return host === 'localhost' || host === '[::1]' || (octets.length === 4 && octets[0] === '127'
      && octets.every(part => /^\d{1,3}$/.test(part) && Number(part) <= 255));
  }
  catch { return false; }
};

function SettingsDialog({ children, onClose, returnFocusTo, fallbackFocusTo }: {
  children: ReactNode; onClose: () => void; returnFocusTo: RefObject<HTMLElement | null>;
  fallbackFocusTo: RefObject<HTMLButtonElement | null>;
}) {
  const { t } = useI18n();
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const element = dialog.current;
    element?.showModal();
    return () => {
      element?.close();
      const target = returnFocusTo.current?.isConnected ? returnFocusTo.current : fallbackFocusTo.current;
      target?.focus();
    };
  }, [returnFocusTo, fallbackFocusTo]);
  return <dialog ref={dialog} className="settings-dialog" aria-label={t('settings')}
    onCancel={event => { event.preventDefault(); onClose(); }}
    onClick={event => { if (event.target === event.currentTarget) onClose(); }}>{children}</dialog>;
}

function Connections({ profiles, selected, profileCatalog, onCatalog, onSaved, onClose, maxCalls, onMaxCallsChange }: {
  profiles: Profile[]; selected: string; onSaved: (id: string) => Promise<void>; onClose: () => void;
  profileCatalog: ModelCatalog | null; onCatalog: (profileId: string, catalog: ModelCatalog | null) => void;
  maxCalls: number; onMaxCallsChange: (value: number) => void;
}) {
  const { t } = useI18n();
  const blank = { name: '', base_url: 'http://127.0.0.1:8080/v1', model: '', vision: false, local: true,
    reasoning_effort: null as string | null };
  const [editing, setEditing] = useState(selected);
  const [form, setForm] = useState(() => profiles.find(p => p.id === selected) ?? blank);
  const lookup = useReasoning(form.model);
  const [catalog, setCatalog] = useState<ModelCatalog | null>(profileCatalog);
  const catalogRef = useRef<ModelCatalog | null>(profileCatalog);
  const [key, setKey] = useState('');
  const [clearKey, setClearKey] = useState(false);
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [pythonAllowed, setPythonAllowed] = useState<boolean | null>(null);
  const [savingPermission, setSavingPermission] = useState(false);
  const closeButton = useRef<HTMLButtonElement>(null);
  useEffect(() => { closeButton.current?.focus(); }, []);
  useEffect(() => {
    const controller = new AbortController();
    api<{ python_execution_allowed: boolean }>('/settings', { signal: controller.signal })
      .then(result => setPythonAllowed(result.python_execution_allowed))
      .catch(error => { if (!controller.signal.aborted) setError(errorText(error, t)); });
    return () => controller.abort();
  }, []);

  async function changePythonPermission(allowed: boolean) {
    const previous = pythonAllowed;
    setPythonAllowed(allowed);
    setSavingPermission(true); setError('');
    try {
      const result = await api<{ python_execution_allowed: boolean }>('/settings', {
        method: 'PUT', body: JSON.stringify({ python_execution_allowed: allowed }),
      });
      setPythonAllowed(result.python_execution_allowed);
    } catch (error) { setPythonAllowed(previous); setError(errorText(error, t)); }
    finally { setSavingPermission(false); }
  }

  function edit(id: string) {
    setEditing(id); setForm(profiles.find(p => p.id === id) ?? blank);
    const nextCatalog = id === selected ? profileCatalog : null;
    setCatalog(nextCatalog); catalogRef.current = nextCatalog;
    setKey(''); setClearKey(false); setError(''); setNotice('');
  }
  function modelChanged(model: string) {
    const modelInfo = catalog?.models.find(item => item.id === model);
    const metadata = modelInfo?.reasoning;
    const selectedValue = form.reasoning_effort;
    const keep = model === form.model || !catalog ||
      Boolean(metadata?.supported_efforts?.includes(selectedValue ?? ''));
    setForm({ ...form, model, reasoning_effort: keep ? selectedValue : null });
  }
  function catalogChanged(next: ModelCatalog) {
    setCatalog(next); catalogRef.current = next;
  }
  const metadata = catalog?.models.find(item => item.id === form.model)?.reasoning ?? lookup.reasoning;
  const options = reasoningOptionsFor(metadata, t);
  async function save(event: FormEvent) {
    event.preventDefault();
    if (lookup.loading && !metadata) return;
    setBusy(true); setError(''); setNotice('');
    try {
      const savedReasoning = selectedReasoning(metadata, form.reasoning_effort, t);
      const body = { name: form.name, base_url: form.base_url, model: form.model,
        local: form.local, vision: form.vision, reasoning_effort: savedReasoning || null,
        ...(key || clearKey ? { api_key: clearKey ? '' : key } : {}) };
      const result = await api<Profile>(editing ? `/profiles/${editing}` : '/profiles', {
        method: editing ? 'PUT' : 'POST', body: JSON.stringify(body),
      });
      const previous = profiles.find(profile => profile.id === editing);
      const keyRemoved = previous?.has_key && previous.base_url !== result.base_url && !result.has_key;
      setEditing(result.id); setForm(result); setKey(''); setClearKey(false);
      onCatalog(result.id, catalogRef.current);
      await onSaved(result.id);
      setNotice(keyRemoved
        ? t('connectionSavedKeyRemoved')
        : t('connectionSaved'));
    } catch (error) { setError(errorText(error, t)); } finally { setBusy(false); }
  }
  async function probe() {
    setBusy(true); setError(''); setNotice('');
    try {
      const result = await api<{ message: string }>(`/profiles/${editing}/test`, { method: 'POST' });
      setNotice(result.message);
    } catch (error) { setError(errorText(error, t)); } finally { setBusy(false); }
  }
  async function remove() {
    setBusy(true); setError('');
    try { await api(`/profiles/${editing}`, { method: 'DELETE' }); await onSaved(''); edit(''); }
    catch (error) { setError(errorText(error, t)); } finally { setBusy(false); }
  }
  return <section id="app-settings" className="settings panel" aria-label={t('settings')}>
    <div className="section-title"><h2>{t('settings')}</h2>
      <button ref={closeButton} onClick={onClose}>{t('closeSettings')}</button></div>
    <p className="local-badge">{t('localOnly')}</p>
    <h3>{t('connections')}</h3>
    <p className="muted">{t('connectionsHint')}</p>
    <div className="settings-layout">
      <nav aria-label={t('savedConnections')}>
        {profiles.map(profile => <button key={profile.id} className={editing === profile.id ? 'selected' : ''}
          onClick={() => edit(profile.id)}>{profile.name}<small>{profile.model}</small></button>)}
        <button onClick={() => edit('')}>{t('newConnection')}</button>
      </nav>
      <form onSubmit={save}>
        <label>{t('connectionName')}<input required maxLength={200} value={form.name}
          onChange={e => setForm({ ...form, name: e.target.value })} /></label>
        <label>{t('apiAddress')}<input required type="url" value={form.base_url} placeholder="https://api.example.com/v1"
          onChange={e => { setCatalog(null); catalogRef.current = null;
            setForm({ ...form, base_url: e.target.value, local: localAddress(e.target.value) }); }} /></label>
        <label>{t('apiKey')}<input type="password" autoComplete="new-password" value={key}
          placeholder={profiles.find(p => p.id === editing)?.has_key ? t('savedKeyPlaceholder') : t('localKeyPlaceholder')}
          onChange={e => { setKey(e.target.value); setClearKey(false); }} /></label>
        {profiles.find(p => p.id === editing)?.has_key ? <label className="check"><input type="checkbox"
          checked={clearKey} onChange={e => setClearKey(e.target.checked)} />{t('removeSavedKey')}</label> : null}
        <ModelPicker baseUrl={form.base_url} profileId={editing} value={form.model} disabled={busy}
          apiKey={clearKey ? '' : key || undefined} onChange={modelChanged} onCatalog={catalogChanged} />
        <label>{t('reasoningEffort')}<select value={selectedReasoning(metadata, form.reasoning_effort, t)} disabled={busy || (lookup.loading && !metadata)}
          aria-describedby="reasoning-hint"
          onChange={e => setForm({ ...form, reasoning_effort: e.target.value || null })}>
          {options.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select></label>
        <p id="reasoning-hint" className="hint">{lookup.hint} {t('reasoningSavedHint')}</p>
        <button type="button" disabled={busy || !form.model.trim()} onClick={lookup.refresh}>{t('refreshLevels')}</button>
        <label className="check"><input type="checkbox" checked={form.vision}
          onChange={e => setForm({ ...form, vision: e.target.checked })} />{t('imageSupport')}</label>
        <label className="check"><input type="checkbox" checked={form.local}
          onChange={e => setForm({ ...form, local: e.target.checked })} />{t('localConnection')}</label>
        <p className="hint">{t('imagePolicyHint')}</p>
        {notice ? <p role="status" className="notice">{notice}</p> : null}
        {error ? <p role="alert" className="error">{error}</p> : null}
        <div className="actions"><button className="primary" disabled={busy || (lookup.loading && !metadata)} type="submit">{t('saveConnection')}</button>
          <button disabled={busy || !editing} type="button" onClick={probe}>{t('probeConnection')}</button>
          <button className="danger" disabled={busy || !editing} type="button" onClick={remove}>{t('deleteConnection')}</button>
        </div>
      </form>
    </div>
    <div className="run-settings">
      <h3>{t('requestSettings')}</h3>
      <label className="check"><input type="checkbox" checked={pythonAllowed === true}
        disabled={pythonAllowed === null || savingPermission}
        onChange={event => void changePythonPermission(event.target.checked)} />{t('alwaysAllowPython')}</label>
      <p className="hint">{t('pythonPermissionHint')}</p>
      <label>{t('callLimit')}<input type="number" min={1} max={1000} value={maxCalls}
        onChange={e => onMaxCallsChange(Number(e.target.value))} /></label>
    </div>
  </section>;
}

function ConfirmationPanel({ confirmation, onAnswer }: {
  confirmation: Confirmation; onAnswer: (approved: boolean) => Promise<void>;
}) {
  const { t } = useI18n();
  const [busy, setBusy] = useState(false);
  const firstButton = useRef<HTMLButtonElement>(null);
  useEffect(() => { firstButton.current?.focus(); }, []);
  async function answer(approved: boolean) {
    setBusy(true);
    try { await onAnswer(approved); } finally { setBusy(false); }
  }
  return <section role="dialog" aria-label={t('confirmationAria')}
    className="confirmation panel">
    <div className="eyebrow">{t('confirmationEyebrow')}</div>
    <h2>{t('confirmationTitle')}</h2>
    <p>{t('confirmationText')}</p>
    <pre tabIndex={0}>{confirmation.code}</pre>
    <div className="actions"><button ref={firstButton} className="primary" disabled={busy}
      onClick={() => answer(true)}>{t('allowPermanently')}</button>
      <button disabled={busy} onClick={() => answer(false)}>{t('reject')}</button></div>
  </section>;
}

function OperationLog({ events }: { events: RunEvent[] }) {
  const { t } = useI18n();
  const starts = events.filter(e => e.type === 'tool_start');
  const results = new Map(events.filter(e => e.type === 'tool_result').map(e => [e.data.tool_call_id, e]));
  const list = useRef<HTMLDivElement>(null);
  const following = useRef(true);
  function showLatest() {
    if (list.current && following.current) list.current.scrollTop = list.current.scrollHeight;
  }
  useEffect(showLatest, [events.length]);
  return <details className="operations" onToggle={event => {
    if (event.target === event.currentTarget && event.currentTarget.open) {
      showLatest();
      list.current?.scrollIntoView({ block: 'nearest' });
    }
  }}><summary role="button">{t('operationLog')}</summary>
    {starts.length === 0 ? <p className="muted">{t('noToolsYet')}</p>
      : <div className="operation-list" ref={list} role="region" aria-label={t('cadOperations')} tabIndex={0}
        onScroll={event => {
          const el = event.currentTarget;
          following.current = el.scrollHeight - el.clientHeight - el.scrollTop < 30;
        }}>{starts.map(start => {
      const id = start.data.tool_call_id;
      const result = results.get(id);
      const value = result?.data.result as { ok?: boolean; uncertain?: boolean; verification?: { status: string } } | undefined;
      const name = String(start.data.name);
      return <details key={start.seq}><summary><span>{toolKeys[name] ? t(toolKeys[name]) : t('cadOperation')} · <code>{name}</code></span>
        <small>{start.data.verification ? t('verificationPrefix') : ''}{result ? (value?.uncertain ? t('unknownOutcome') : value?.ok ? t('done') : t('error')) : t('waiting')}</small></summary>
        <pre tabIndex={0}>{JSON.stringify({ arguments: start.data.arguments, result: result?.data.result }, null, 2)}</pre>
      </details>;
    })}</div>}
  </details>;
}

function RunView({ run }: { run: Run }) {
  const { t } = useI18n();
  return <article className="run" aria-label={t('request')}>
    {run.events.filter(event => ['message', 'image', 'error'].includes(event.type)).map(event => {
      if (event.type === 'message') return <div key={event.seq}
        className={`message ${event.data.role === 'user' ? 'user' : 'assistant'}`}>
        <div className="message-heading"><span className="message-author">{event.data.role === 'user' ? t('you') : t('cadAgent')}</span>
          {String(event.data.content) ? <CopyButton text={String(event.data.content)}
            label={event.data.role === 'user' ? t('copyRequest') : t('copyAnswer')} /> : null}</div>
        {event.data.role === 'user' ? <>
          {event.data.content ? <p className="user-text">{String(event.data.content)}</p> : null}
          {Array.isArray(event.data.attachments) ? <div className="user-attachments">
            {(event.data.attachments as Attachment[]).map((image, index) => <a key={index}
              href={`data:${image.mimeType};base64,${image.data}`} download={image.name}>
              <img src={`data:${image.mimeType};base64,${image.data}`} alt={image.name} />
              <span>{image.name}</span></a>)}
          </div> : null}</>
          : <MessageContent text={String(event.data.content)} />}
      </div>;
      if (event.type === 'image') return <figure key={event.seq}><a target="_blank" rel="noreferrer"
        href={`data:${event.data.mimeType};base64,${event.data.data}`} download="solidworks.png">
        <img src={`data:${event.data.mimeType};base64,${event.data.data}`} alt={t('solidworksScreenshot')} /></a>
        <figcaption>{t('screenshotFromSolidworks')}</figcaption></figure>;
      return <p key={event.seq} className="error">{String(event.data.message)}</p>;
    })}
    <div className="run-meta"><span className={`badge ${run.status}`} data-testid="run-status">{statusKeys[run.status] ? t(statusKeys[run.status]) : run.status}</span>
      <span>{t('callsCount', { count: run.call_count })}</span>
      {run.verification === 'unverified' ? <strong className="warning">{t('resultNeedsReview')}</strong> : null}
      {run.verification === 'verified' ? <span>{t('checksPassed')}</span> : null}
      {run.verification === 'failed' ? <strong className="error">{t('checkFailed')}</strong> : null}
    </div>
    {run.status === 'stopping' ? <p className="hint">{t('stopHint')}</p> : null}
    {run.status === 'uncertain' ? <p className="error">{t('uncertainHint')}</p> : null}
    <OperationLog events={run.events} />
  </article>;
}

export default function App() {
  const { language, setLanguage, t } = useI18n();
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [selected, setSelected] = useState(() => stored('profile', ''));
  const [sessionId, setSessionId] = useState(() => stored('session', crypto.randomUUID()));
  const [runs, setRuns] = useState<Run[]>([]);
  const runsRef = useRef(runs);
  runsRef.current = runs;
  const [streamId, setStreamId] = useState('');
  const [settings, setSettings] = useState(false);
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [message, setMessage] = useState('');
  const [attachments, setAttachments] = useState<DraftAttachment[]>([]);
  const [readingImages, setReadingImages] = useState(false);
  const [savingProfile, setSavingProfile] = useState(false);
  const [pendingActivity, setPendingActivity] = useState<PendingActivity | null>(null);
  const [runContact, setRunContact] = useState<RunContact | null>(null);
  const [catalogs, setCatalogs] = useState<Record<string, ModelCatalog>>({});
  const [error, setError] = useState('');
  const [maxCalls, setMaxCalls] = useState(80);
  const end = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const settingsOpener = useRef<HTMLElement | null>(null);
  const settingsButton = useRef<HTMLButtonElement>(null);
  const pendingRequest = useRef('');
  const fileInput = useRef<HTMLInputElement>(null);
  const draftRevision = useRef(0);
  const readingLock = useRef(false);
  const profileLock = useRef(false);
  const active = runs.find(run => !terminal(run.status));
  const uncertain = runs.some(run => run.status === 'uncertain');
  const currentProfile = profiles.find(profile => profile.id === selected);
  const lookup = useReasoning(currentProfile?.model ?? '');
  const currentReasoning = catalogs[selected]?.models.find(model => model.id === currentProfile?.model)?.reasoning
    ?? lookup.reasoning;
  const currentReasoningOptions = reasoningOptionsFor(currentReasoning, t);
  const currentReasoningValue = selectedReasoning(currentReasoning, currentProfile?.reasoning_effort, t);
  const imagesAllowed = currentProfile?.vision === true;
  const canSend = !loading && !sending && !active && !!currentProfile && !uncertain && !readingImages && !savingProfile
    && !(lookup.loading && !currentReasoning)
    && (!!message.trim() || attachments.length > 0) && (!attachments.length || imagesAllowed);

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
        if (unfinished) {
          setRunContact({ runId: unfinished, at: Date.now(), failed: false });
          setStreamId(unfinished);
        }
      }).catch(error => { if (!disposed) setError(errorText(error, t)); })
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
    let current = runsRef.current.find(run => run.id === streamId);
    if (!current || terminal(current.status)) return;
    let disposed = false;
    let refreshing = false;
    let finished = false;
    let lastContact = Date.now();
    let lastRecovery = 0;
    let controller: AbortController | null = null;
    const after = current.events.at(-1)?.seq ?? 0;
    const source = new EventSource(`/api/runs/${streamId}/events?after=${after}`);
    function contacted() {
      lastContact = Date.now();
      setRunContact({ runId: streamId, at: lastContact, failed: false });
    }
    async function refresh() {
      if (disposed || refreshing || finished) return;
      refreshing = true;
      lastRecovery = Date.now();
      controller = new AbortController();
      const timeout = window.setTimeout(() => controller?.abort(), 5000);
      try {
        const snapshot = await api<Run>(`/runs/${streamId}`, { signal: controller.signal });
        if (disposed) return;
        if (!current || current.events.length <= snapshot.events.length) current = snapshot;
        merge(snapshot);
        contacted();
        if (terminal(snapshot.status)) { finished = true; source.close(); }
      } catch {
        if (!disposed) setRunContact(previous => ({ runId: streamId,
          at: previous?.runId === streamId ? previous.at : Date.now(), failed: true }));
      } finally { clearTimeout(timeout); refreshing = false; controller = null; }
    }
    source.addEventListener('update', event => {
      if (disposed || finished) return;
      try {
        const restored = runsRef.current.find(run => run.id === streamId);
        if (restored && (!current || restored.events.length > current.events.length)) current = restored;
        const update = JSON.parse((event as MessageEvent).data) as RunEvent;
        if (!current || !update.state) { void refresh(); return; }
        current = applyRunEvent(current, update);
        merge(current);
        contacted();
        if (terminal(current.status)) { finished = true; source.close(); }
      } catch { void refresh(); }
    });
    source.addEventListener('heartbeat', event => {
      if (disposed || finished) return;
      try {
        JSON.parse((event as MessageEvent).data);
        contacted();
        // The producer may be ahead of queued SSE updates; only update seq detects gaps.
      } catch { void refresh(); }
    });
    source.onopen = contacted;
    source.onerror = () => { void refresh(); };
    // A quiet healthy stream sends heartbeats; snapshots are recovery only.
    const watchdog = window.setInterval(() => {
      if (Date.now() - lastContact >= 8000 && Date.now() - lastRecovery >= 3000) void refresh();
    }, 1000);
    const leave = () => { navigator.sendBeacon(`/api/runs/${streamId}/stop`); };
    window.addEventListener('pagehide', leave);
    return () => {
      disposed = true; source.close(); clearInterval(watchdog); controller?.abort();
      window.removeEventListener('pagehide', leave);
    };
  }, [streamId]);

  useEffect(() => { end.current?.scrollIntoView({ block: 'nearest' }); }, [runs.at(-1)?.events.length]);
  useEffect(() => {
    if (!input.current) return;
    input.current.style.height = 'auto';
    input.current.style.height = `${Math.min(input.current.scrollHeight, 180)}px`;
  }, [message]);

  async function reloadProfiles(id: string) {
    const profiles = await api<Profile[]>('/profiles'); setProfiles(profiles);
    const selected = id || profiles[0]?.id || ''; setSelected(selected); remember('profile', selected);
  }
  function rememberCatalog(profileId: string, catalog: ModelCatalog | null) {
    if (!profileId) return;
    setCatalogs(previous => {
      if (catalog) return { ...previous, [profileId]: catalog };
      const next = { ...previous }; delete next[profileId]; return next;
    });
  }
  async function changeReasoning(value: string) {
    if (!selected || profileLock.current) return;
    profileLock.current = true; setSavingProfile(true); setError('');
    try {
      const updated = await api<Profile>(`/profiles/${selected}`, { method: 'PUT',
        body: JSON.stringify({ reasoning_effort: value || null }) });
      setProfiles(previous => previous.map(profile => profile.id === updated.id ? updated : profile));
    } catch (error) { setError(errorText(error, t)); }
    finally { profileLock.current = false; setSavingProfile(false); }
  }
  async function attach(files: File[]) {
    if (!files.length || !imagesAllowed || readingLock.current || sending || active) return;
    readingLock.current = true; setReadingImages(true); setError('');
    setPendingActivity({ phase: 'images', at: Date.now(), files: files.length + attachments.length,
      bytes: [...files, ...attachments].reduce((sum, image) => sum + image.size, 0) });
    const revision = draftRevision.current;
    try {
      const images = await readImages(files, attachments);
      if (revision === draftRevision.current) setAttachments(previous => [...previous, ...images]);
    } catch (error) { if (revision === draftRevision.current) setError(errorText(error, t)); }
    finally {
      readingLock.current = false; setReadingImages(false);
      if (revision === draftRevision.current) setPendingActivity(null);
    }
  }
  async function send(event: FormEvent) {
    event.preventDefault(); if (!canSend) return;
    setSending(true); setError('');
    setPendingActivity({ phase: 'sending', at: Date.now(), files: attachments.length,
      bytes: attachments.reduce((sum, image) => sum + image.size, 0) });
    pendingRequest.current = crypto.randomUUID();
    try {
      if (currentProfile && (currentReasoningValue || null) !== currentProfile.reasoning_effort) {
        const updated = await api<Profile>(`/profiles/${selected}`, { method: 'PUT',
          body: JSON.stringify({ reasoning_effort: currentReasoningValue || null }) });
        setProfiles(previous => previous.map(profile => profile.id === updated.id ? updated : profile));
      }
      const run = await api<Run>('/runs', { method: 'POST', body: JSON.stringify({
        session_id: sessionId, profile_id: selected, message, max_calls: maxCalls, language,
        attachments: attachments.map(({ name, mimeType, data }) => ({ name, mimeType, data })),
        request_id: pendingRequest.current,
      }) });
      merge(run); setRunContact({ runId: run.id, at: Date.now(), failed: false });
      setStreamId(run.id); setMessage(''); setAttachments([]); remember('session', sessionId);
    } catch (error) { setError(errorText(error, t)); } finally { setSending(false); setPendingActivity(null); }
  }
  async function stop() {
    if (!active) return;
    try { merge(await api<Run>(`/runs/${active.id}/stop`, { method: 'POST' })); }
    catch (error) { setError(errorText(error, t)); }
  }
  async function confirm(approved: boolean) {
    if (!active?.confirmation) return;
    try { merge(await api<Run>(`/runs/${active.id}/confirm`, { method: 'POST', body: JSON.stringify({
      confirmation_id: active.confirmation.id, approved,
    }) })); } catch (error) { setError(errorText(error, t)); }
  }
  function newDialogue() {
    const id = crypto.randomUUID(); setSessionId(id); remember('session', id);
    setStreamId(''); setRuns([]); setError('');
    draftRevision.current++; setAttachments([]); setMessage(''); setPendingActivity(null); setRunContact(null);
    input.current?.focus();
  }
  function openSettings() {
    if (profileLock.current) return;
    settingsOpener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setSettings(true);
  }

  return <div className="shell">
    <header className="app-header"><div className="header-context">
      <div className="brand"><div className="brand-icon"><Icon name="cube" size={24} /></div>
        <h1>CAD-Agent-Next</h1></div></div>
      <div className="header-actions">
        <label className="language-switcher"><span className="sr-only">{t('language')}</span>
          <select aria-label={t('language')} value={language}
            onChange={event => setLanguage(event.target.value as Language)}>
            {languages.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}
          </select></label>
        <button className="header-button" aria-label={t('newDialogue')} disabled={!!active || sending} onClick={newDialogue}>
          <Icon name="plus" size={18} /><span className="button-label">{t('newDialogue')}</span></button>
        <button ref={settingsButton} className="header-button" aria-label={t('settings')} aria-expanded={settings} aria-controls="app-settings"
          disabled={savingProfile} onClick={openSettings}><Icon name="settings" size={18} />
          <span className="button-label">{t('settings')}</span></button></div></header>
    <main>
      {error ? <p role="alert" className="error panel">{error}</p> : null}
      <section className="chat" aria-label={t('chat')} tabIndex={0}>
        {loading ? <p role="status">{t('loadingState')}</p> : null}
        {!loading && runs.length === 0 ? <div className="empty"><div className="empty-icon"><Icon name="cube" size={36} /></div>
          <div className="eyebrow">{t('assistantEyebrow')}</div>
          <h2>{t('emptyTitle')}</h2><p>{t('emptyText')}</p>
          {!profiles.length ? <button className="primary" onClick={openSettings}>{t('addConnection')}</button> : null}
          <div className="suggestions">{([
            ['tree', t('suggestionTreeTitle'), t('suggestionTreeText')],
            ['image', t('suggestionImageTitle'), t('suggestionImageText')],
            ['ruler', t('suggestionRulerTitle'), t('suggestionRulerText')],
          ] as const).map(([icon, title, text]) => <button type="button" key={icon} onClick={() => {
            setMessage(text); input.current?.focus();
          }}><Icon name={icon} size={19} />{title}</button>)}</div>
        </div> : null}
        {runs.map(run => <RunView key={run.id} run={run} />)}
        {active?.confirmation ? <ConfirmationPanel key={active.confirmation.id}
          confirmation={active.confirmation} onAnswer={confirm} /> : null}
        <div ref={end} />
      </section>
      <ActivityIndicator run={active ?? runs.at(-1)} pending={pendingActivity} contact={runContact} />
      <form className="composer" aria-label={t('messageForm')} onSubmit={send}>
        {attachments.length ? <div className="attachment-previews" role="region" aria-label={t('attachmentsRegion')}>
          {attachments.map((image, index) => <div className="attachment-preview" key={index}>
            <img src={`data:${image.mimeType};base64,${image.data}`} alt={image.name} />
            <span title={image.name}>{image.name}</span>
            <button type="button" aria-label={t('removeAttachment', { name: image.name })} disabled={sending || readingImages}
              onClick={() => setAttachments(previous => previous.filter((_, i) => i !== index))}>
              <Icon name="close" size={14} /></button></div>)}
        </div> : null}
        <div className="composer-label"><label className="sr-only" htmlFor="chat-message">{t('message')}</label>
          <textarea id="chat-message" ref={input} rows={1} value={message} maxLength={20000}
          onChange={e => setMessage(e.target.value)} placeholder={t('composerPlaceholder')}
          onKeyDown={event => {
            if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault();
              if (canSend) event.currentTarget.form?.requestSubmit();
            }
          }}
          disabled={loading} /></div>
        <div className="composer-footer">
          <div className="composer-controls">
            <input ref={fileInput} type="file" hidden multiple accept={imageTypes.join(',')} aria-label={t('imageFiles')}
              disabled={!imagesAllowed || loading || sending || !!active || readingImages}
              onChange={event => {
                const files = Array.from(event.currentTarget.files ?? []);
                event.currentTarget.value = ''; void attach(files);
              }} />
            <button type="button" className="attach-button" aria-label={t('attachImage')}
              title={imagesAllowed ? t('attachTitle') : t('attachUnsupported')}
              disabled={!imagesAllowed || loading || sending || !!active || readingImages} onClick={() => fileInput.current?.click()}>
              <Icon name="paperclip" size={20} /></button>
            <label className="model-switcher"><span className="sr-only">{t('activeConnection')}</span><select value={selected}
              title={t('model')} onChange={e => { setSelected(e.target.value); remember('profile', e.target.value); }}
              disabled={loading || sending || savingProfile || !!active}>
              <option value="">{t('chooseModel')}</option>{profiles.map(profile => <option key={profile.id}
                value={profile.id}>{profile.model} · {profile.name}</option>)}</select></label>
            <label className="reasoning-switcher"><span className="sr-only">{t('reasoningEffort')}</span>
              <select value={currentReasoningValue} title={t('reasoningEffort')}
                disabled={loading || sending || savingProfile || !currentProfile || !!active || (lookup.loading && !currentReasoning)}
                onChange={event => void changeReasoning(event.target.value)}>
                {currentReasoningOptions.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </select></label>
          </div>
          <div className="actions">{active ? <button type="button" className="stop-button" onClick={stop}
            aria-label={t('stop')} title={t('stop')} disabled={active.status === 'stopping'}><Icon name="stop" size={18} /></button>
            : <button className="send-button" type="submit" aria-label={t('send')} title={sending ? t('sending') : t('send')} disabled={!canSend}>
              <Icon name="send" size={22} /></button>}</div></div>
        <p className="composer-hint" title={lookup.hint}><span>{readingImages ? t('readingImages') : savingProfile ? t('savingChoice')
          : attachments.length && !imagesAllowed ? t('imagesUnsupported')
          : active ? t('working') : t('composerHint')}</span>
          {currentProfile ? ` · ${lookup.hint}` : ''}</p>
      </form>
    </main>
    {settings ? <SettingsDialog onClose={() => setSettings(false)} returnFocusTo={settingsOpener} fallbackFocusTo={settingsButton}>
      <Connections profiles={profiles} selected={selected} profileCatalog={catalogs[selected] ?? null}
      onCatalog={rememberCatalog} onSaved={reloadProfiles}
      maxCalls={maxCalls} onMaxCallsChange={setMaxCalls} onClose={() => setSettings(false)} /></SettingsDialog> : null}
  </div>;
}
