import { useEffect, useRef, useState } from 'react';
import { api, uploadBinary } from './api';
import type { MeshAssetState, MeshSelection } from './api';

export default function MeshReference({ sessionId, disabled, onChange, onBusy }: {
  sessionId: string; disabled: boolean; onChange: (value: MeshSelection | null) => void;
  onBusy: (value: boolean) => void;
}) {
  const [units, setUnits] = useState('');
  const [tolerance, setTolerance] = useState('0.1');
  const [state, setState] = useState<MeshAssetState>({ state: 'empty' });
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState('');
  const controller = useRef<AbortController | null>(null);
  const version = useRef(0);
  const toleranceRef = useRef(tolerance);
  toleranceRef.current = tolerance;
  const path = `/projects/${encodeURIComponent(sessionId)}/mesh`;
  const readyAsset = state.state === 'ready' ? state.asset_id : state.current?.asset_id;
  const bounds = state.summary?.bounds_mm;

  function publish(value: MeshAssetState) {
    setState(value);
    const asset = value.state === 'ready' ? value.asset_id : value.current?.asset_id;
    onChange(asset ? { asset_id: asset, tolerance_mm: Number(toleranceRef.current) } : null);
  }

  useEffect(() => {
    const current = ++version.current;
    const abort = new AbortController();
    controller.current = abort;
    onBusy(true); setBusy(true);
    (async () => {
      while (!abort.signal.aborted && version.current === current) {
        const value = await api<MeshAssetState>(path, { signal: abort.signal });
        if (version.current !== current) return;
        publish(value);
        if (!['processing', 'uploading'].includes(value.state)) break;
        await new Promise(resolve => setTimeout(resolve, 250));
      }
    })().catch(() => { /* A later upload can retry an unavailable status endpoint. */ })
      .finally(() => { if (version.current === current) { onBusy(false); setBusy(false); } });
    return () => { version.current++; abort.abort(); onBusy(false); };
    // The session owns this component; setters passed by the parent are stable.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path]);

  async function choose(file?: File) {
    if (!file || busy || disabled) return;
    const format = file.name.split('.').at(-1)?.toLowerCase();
    if (!['stl', 'obj'].includes(format ?? '') || !['mm', 'in'].includes(units)) {
      setError('Выберите STL/OBJ и укажите единицы сетки.'); return;
    }
    if (file.size > 100 * 1024 * 1024) { setError('Файл сетки превышает 100 МиБ.'); return; }
    const current = ++version.current;
    controller.current?.abort();
    const abort = new AbortController();
    controller.current = abort;
    onBusy(true); setBusy(true); setError(''); setProgress(0);
    try {
      const value = await uploadBinary<MeshAssetState>(`${path}?format=${format}&units=${units}`, file,
        abort.signal, fraction => { if (version.current === current) setProgress(fraction); });
      if (version.current !== current) return;
      publish({ ...value, current: state.current });
      while (!abort.signal.aborted && version.current === current) {
        const result = await api<MeshAssetState>(path, { signal: abort.signal });
        if (version.current !== current) return;
        publish(result);
        if (result.state !== 'processing' && result.state !== 'uploading') {
          if (result.state === 'error') setError(result.error ?? 'Ошибка обработки сетки.');
          break;
        }
        await new Promise(resolve => setTimeout(resolve, 250));
      }
    } catch (error) {
      if (version.current === current && !abort.signal.aborted)
        setError(error instanceof Error ? error.message : 'Ошибка загрузки сетки.');
    } finally {
      if (version.current === current) { onBusy(false); setBusy(false); }
    }
  }

  async function remove() {
    version.current++; controller.current?.abort();
    try { await api(path, { method: 'DELETE' }); publish({ state: 'empty' }); setError(''); }
    catch (error) { setError(error instanceof Error ? error.message : 'Не удалось удалить эталон.'); }
    finally { onBusy(false); setBusy(false); }
  }

  return <details className="mesh-reference">
    <summary>Эталон STL/OBJ {readyAsset ? '· прикреплён' : ''}</summary>
    <div className="mesh-controls">
      <label>Единицы сетки<select aria-label="Единицы сетки" value={units} disabled={disabled || busy}
        onChange={event => setUnits(event.target.value)}>
        <option value="">Выберите единицы</option><option value="mm">Миллиметры</option><option value="in">Дюймы</option>
      </select></label>
      <label>Допуск, мм<input type="number" aria-label="Допуск сетки, мм" min="0.000001" max="10" step="any"
        value={tolerance} disabled={disabled} onChange={event => {
          setTolerance(event.target.value);
          onChange(readyAsset ? { asset_id: readyAsset, tolerance_mm: Number(event.target.value) } : null);
        }} /></label>
      <label className="mesh-file">Файл STL или OBJ<input type="file" aria-label="Файл STL или OBJ" accept=".stl,.obj"
        disabled={disabled || busy || !units} onChange={event => {
          const file = event.currentTarget.files?.[0]; event.currentTarget.value = ''; void choose(file);
        }} /></label>
      {busy || readyAsset ? <button type="button" disabled={disabled} onClick={() => void remove()}>
        {busy ? 'Отменить обработку сетки' : 'Удалить эталон'}</button> : null}
    </div>
    <p data-testid="mesh-state" role="status">{busy ? `Обработка сетки · загрузка ${Math.round(progress*100)}%`
      : state.state === 'ready' ? 'Готово' : state.state === 'error' ? 'Ошибка обработки' : 'Эталон не выбран'}</p>
    {bounds ? <p data-testid="mesh-bounds">Габариты: {bounds[1].map((v, i) =>
      Number((v-bounds[0][i]).toFixed(4))).join(' × ')} мм · {state.summary?.triangles} треугольников.
      {state.summary?.watertight && state.summary.winding_consistent ? ' Замкнутая сетка.' : ' Сетка неполная: итоговая проверка ограничена.'}</p> : null}
    {error ? <p role="alert" className="error">{error}</p> : null}
  </details>;
}
