import { useEffect, useId, useRef, useState } from 'react';
import { api } from './api';

interface Props {
  baseUrl: string; profileId: string; apiKey?: string;
  value: string; onChange: (value: string) => void; disabled: boolean;
}

export default function ModelPicker({ baseUrl, profileId, apiKey, value, onChange, disabled }: Props) {
  const id = useId();
  const input = useRef<HTMLInputElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const request = useRef<AbortController | null>(null);
  const loaded = useRef(false);
  const [models, setModels] = useState<string[]>([]);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [active, setActive] = useState(-1);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const matching = models.filter(name => name.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));

  useEffect(() => {
    loaded.current = false; request.current = null;
    setModels([]); setLoading(false); setError(''); setOpen(false); setQuery(''); setActive(-1);
    return () => { request.current?.abort(); };
  }, [baseUrl, profileId, apiKey]);

  useEffect(() => {
    if (open && active >= 0) list.current?.children[active]?.scrollIntoView({ block: 'nearest' });
  }, [active, open]);

  async function load(refresh = false) {
    if (request.current || (loaded.current && !refresh)) return;
    const controller = new AbortController();
    request.current = controller; setLoading(true); setError('');
    try {
      const result = await api<string[]>('/models', { method: 'POST', signal: controller.signal,
        body: JSON.stringify({ base_url: baseUrl, ...(profileId ? { profile_id: profileId } : {}),
          ...(apiKey !== undefined ? { api_key: apiKey } : {}) }),
      });
      if (controller.signal.aborted) return;
      setModels(result); loaded.current = true; setActive(-1);
    } catch (error) {
      if (!controller.signal.aborted) {
        setError(error instanceof Error ? error.message : 'Не удалось загрузить модели.');
      }
    } finally {
      if (request.current === controller) { request.current = null; setLoading(false); }
    }
  }

  function show() {
    setOpen(true); setQuery(''); setActive(-1); void load();
  }
  function choose(name: string) {
    onChange(name); setOpen(false); setActive(-1);
  }

  return <div className="model-picker" onBlur={event => {
    if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false);
  }}>
    <label htmlFor={id}>Имя модели</label>
    <div className="model-input-row">
      <input id={id} ref={input} required maxLength={300} role="combobox" autoComplete="off"
        aria-autocomplete="list" aria-expanded={open} aria-controls={`${id}-list`}
        aria-activedescendant={open && active >= 0 && matching[active] ? `${id}-option-${active}` : undefined}
        aria-describedby={`${id}-hint`} value={value} disabled={disabled}
        placeholder="Выберите модель или введите название"
        onFocus={show} onClick={() => { if (!open) show(); }}
        onChange={event => {
          onChange(event.target.value); setQuery(event.target.value); setOpen(true); setActive(-1);
        }}
        onKeyDown={event => {
          if (event.nativeEvent.isComposing) return;
          if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            event.preventDefault(); void load(); setOpen(true);
            if (!open) {
              setQuery(''); setActive(event.key === 'ArrowDown' ? 0 : models.length - 1);
            } else {
              setActive(previous => matching.length === 0 ? -1 : event.key === 'ArrowDown'
                ? Math.min(previous + 1, matching.length - 1)
                : previous < 0 ? matching.length - 1 : Math.max(previous - 1, 0));
            }
          } else if (event.key === 'Enter' && open) {
            event.preventDefault();
            if (active >= 0 && matching[active]) choose(matching[active]); else setOpen(false);
          } else if (event.key === 'Escape' && open) {
            event.preventDefault(); setOpen(false);
          }
        }} />
      <button type="button" disabled={disabled} aria-label="Показать список моделей"
        onMouseDown={event => event.preventDefault()}
        onClick={() => { if (open) setOpen(false); else { input.current?.focus(); show(); } }}>▾</button>
      {open ? <div className="model-dropdown">
        {loading ? <p role="status">Загружаем модели…</p> : null}
        <div id={`${id}-list`} ref={list} role="listbox" aria-label="Модели API" aria-busy={loading}>
          {matching.map((name, index) => <div role="option" id={`${id}-option-${index}`} key={name}
            aria-selected={active === index} className={active === index ? 'active' : ''}
            onMouseDown={event => event.preventDefault()} onClick={() => choose(name)}>{name}</div>)}
        </div>
        {!loading && !error && matching.length === 0 ? <p role="status">{models.length
          ? 'Модели не найдены. Можно сохранить введённое имя.'
          : 'API вернул пустой список. Введите имя модели вручную.'}</p> : null}
      </div> : null}
    </div>
    <p id={`${id}-hint`} className="hint">Поиск по названию при вводе. Выбор: ↑ ↓ и Enter.</p>
    {error ? <p role="alert" className="error">{error} Имя модели можно ввести вручную.</p> : null}
    <button type="button" className="model-refresh" disabled={disabled || loading}
      onClick={() => {
        input.current?.focus(); setOpen(true); setQuery(''); setActive(-1); void load(true);
      }}>Загрузить модели</button>
  </div>;
}
