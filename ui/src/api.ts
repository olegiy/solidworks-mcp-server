export interface Profile {
  id: string; name: string; base_url: string; model: string;
  vision: boolean; local: boolean; has_key: boolean;
  reasoning_effort: string | null;
}
export interface Confirmation {
  id: string; kind: 'privacy' | 'python'; code?: string;
  base_url?: string; vision?: boolean; message: string;
}
export interface RunEvent {
  seq: number; type: string; data: Record<string, unknown>;
}
export interface Run {
  id: string; session_id: string; profile_id: string; status: string;
  events: RunEvent[]; confirmation: Confirmation | null; call_count: number;
  max_calls: number; verification: string; stop_requested: boolean;
}
export const terminal = (status: string) =>
  ['completed', 'stopped', 'failed', 'uncertain', 'limit_reached', 'rejected'].includes(status);

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...options, headers: { ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...options.headers },
  });
  if (!response.ok) {
    let message = 'Не удалось выполнить запрос к приложению.';
    try { message = (await response.json()).detail ?? message; } catch { /* Keep the local message. */ }
    throw new Error(message);
  }
  return response.status === 204 ? undefined as T : response.json();
}

export function stored(key: string, fallback: string) {
  try { return localStorage.getItem(`cadnext.v1.${key}`) ?? fallback; } catch { return fallback; }
}
export function remember(key: string, value: string) {
  try { localStorage.setItem(`cadnext.v1.${key}`, value); } catch { /* The current tab still works. */ }
}
