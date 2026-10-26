import { acceptLanguage, detectLanguage, tr } from './language';

export interface Profile {
  id: string; name: string; base_url: string; model: string;
  vision: boolean; local: boolean; has_key: boolean;
  reasoning_effort: string | null;
}
export interface ReasoningMetadata {
  mandatory: boolean;
  default_effort: string | null;
  supported_efforts: string[] | null;
}
export interface ModelInfo {
  id: string;
  reasoning: ReasoningMetadata | null;
}
export interface ModelCatalog {
  models: ModelInfo[];
}
export interface Confirmation {
  id: string; kind: 'python'; code?: string; message: string;
}
export interface Attachment {
  name: string; mimeType: string; data: string;
}
export interface RunEvent {
  seq: number; type: string; at?: number; data: Record<string, unknown>;
  state?: Partial<Pick<Run, 'status' | 'call_count' | 'max_calls' | 'verification' |
    'stop_requested' | 'confirmation' | 'repair_attempts' | 'repairing' | 'metrics'>>;
}
export interface Run {
  id: string; session_id: string; profile_id: string; language?: string; status: string;
  events: RunEvent[]; confirmation: Confirmation | null; call_count: number;
  max_calls: number; verification: string; stop_requested: boolean;
  repair_attempts?: number; repairing?: boolean;
  metrics?: Record<string, number | null>;
}
export const terminal = (status: string) =>
  ['completed', 'stopped', 'failed', 'uncertain', 'limit_reached', 'rejected'].includes(status);

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...options, headers: {
      'Accept-Language': acceptLanguage(detectLanguage()),
      ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...options.headers,
    },
  });
  if (!response.ok) {
    let message = tr('Не удалось выполнить запрос к приложению.', 'Could not complete the application request.');
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
