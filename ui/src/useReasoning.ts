import { useEffect, useState } from 'react';
import { api } from './api';
import type { ReasoningMetadata } from './api';

interface Lookup {
  status: 'matched' | 'not_found' | 'ambiguous' | 'unavailable';
  model_id: string | null;
  reasoning: ReasoningMetadata | null;
}

export default function useReasoning(model: string) {
  const [version, setVersion] = useState(0);
  const [state, setState] = useState<{ model: string; result: Lookup | null }>({ model: '', result: null });
  useEffect(() => {
    if (!model.trim()) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      api<Lookup>(`/reasoning?model=${encodeURIComponent(model)}`, { signal: controller.signal })
        .then(result => { if (!controller.signal.aborted) setState({ model, result }); })
        .catch(() => {
          if (!controller.signal.aborted) setState({ model,
            result: { status: 'unavailable', model_id: null, reasoning: null } });
        });
    }, 200);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [model, version]);
  const result = state.model === model ? state.result : null;
  const hint = !model.trim() ? 'Выберите модель для получения уровней OpenRouter.'
    : !result ? 'Загружаем уровни OpenRouter…'
    : result.status === 'not_found' ? 'Соответствие в OpenRouter не найдено. Доступен стандартный список.'
    : result.status === 'ambiguous' ? 'В OpenRouter несколько совпадений. Доступен стандартный список.'
    : result.status === 'unavailable' ? 'Каталог OpenRouter недоступен. Доступен стандартный список.'
    : result.reasoning?.supported_efforts?.length ? `Уровни из OpenRouter: ${result.model_id}.`
    : 'OpenRouter не публикует уровни этой модели. Доступен стандартный список.';
  return { reasoning: result?.reasoning, hint, loading: Boolean(model.trim()) && !result, refresh: () => {
    setState({ model: '', result: null }); setVersion(previous => previous + 1);
  } };
}
