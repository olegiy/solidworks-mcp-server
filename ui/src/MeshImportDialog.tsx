import { useEffect, useRef, useState } from 'react';
import { api, uploadBinary } from './api';

export interface PortableMesh { reference: { sha256: string }; relative_path: string | null }

export default function MeshImportDialog({ mesh, onOpen, onClose }: {
  mesh: PortableMesh; onOpen: (importId?: string, signal?: AbortSignal) => Promise<void>; onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const controller = useRef<AbortController | null>(null);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState('');
  useEffect(() => {
    dialog.current?.showModal();
    return () => { controller.current?.abort(); };
  }, []);
  async function choose(file?: File) {
    if (!file || busy) return;
    if (file.size > 100*1024*1024) { setError('Файл превышает 100 МиБ.'); return; }
    const abort = new AbortController(); controller.current = abort;
    setBusy(true); setError('');
    try {
      const staged = await api<{ import_id: string }>('/conversation-imports', {
        method: 'POST', body: JSON.stringify({ mesh }), signal: abort.signal });
      await uploadBinary(`/conversation-imports/${staged.import_id}/${mesh.reference.sha256}`,
        file, abort.signal, setProgress, 'PUT');
      if (!abort.signal.aborted) await onOpen(staged.import_id, abort.signal);
    } catch (error) {
      if (!abort.signal.aborted) setError(error instanceof Error ? error.message : 'Не удалось открыть проект.');
    } finally { if (!abort.signal.aborted) setBusy(false); }
  }
  async function history() {
    if (busy) return;
    const abort = new AbortController(); controller.current = abort;
    setBusy(true); setError('');
    try { await onOpen(undefined, abort.signal); }
    catch (error) { if (!abort.signal.aborted) setError(String(error)); }
    finally { if (!abort.signal.aborted) setBusy(false); }
  }
  return <dialog ref={dialog} className="mesh-import" aria-label="Исходник сетки проекта"
    onCancel={event => { event.preventDefault(); controller.current?.abort(); onClose(); }}>
    <h2>Исходник сетки проекта</h2>
    <p>Выберите связанный STL/OBJ из каталога assets. Браузер не передаёт соседние файлы автоматически.</p>
    <p>{mesh.relative_path ?? 'Переносимый исходник не был сохранён.'}</p>
    <label>Связанный STL/OBJ<input aria-label="Связанный STL/OBJ" type="file" accept=".stl,.obj"
      disabled={busy || !mesh.relative_path} onChange={event => {
        const file = event.currentTarget.files?.[0]; event.currentTarget.value = ''; void choose(file);
      }} /></label>
    {busy ? <p role="status">Загрузка исходника: {Math.round(progress*100)}%</p> : null}
    {error ? <p role="alert">{error}</p> : null}
    <div className="actions">
      <button type="button" disabled={busy} onClick={() => void history()}>
        Открыть только историю</button>
      <button type="button" onClick={() => { controller.current?.abort(); onClose(); }}>Отмена</button>
    </div>
  </dialog>;
}
