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
export interface GoalOptions {
  enabled: boolean; target_similarity: number; max_evaluations: number;
  max_no_progress: number;
}
export interface GoalRequirement {
  id: string; description: string; source: 'request' | 'attachment';
  required: boolean; weight: number; method: 'cad' | 'vision';
  expected: number | null; tolerance: number | null; unit: 'mm' | 'deg' | null;
}
export interface GoalContract {
  version: 1; kind: 'cad' | 'informational'; requirements: GoalRequirement[];
  reference_ids: string[]; ambiguities: string[]; contract_digest: string;
}
export interface GoalRequirementResult {
  requirement_id: string; description: string; required: boolean; weight: number;
  method: 'cad' | 'vision'; status: 'pass' | 'fail' | 'unknown';
  actual: number | string | null; expected: number | null;
  tolerance: number | null; unit: 'mm' | 'deg' | null;
  source: 'cad' | 'vision' | null; reason: string;
  evidence_id: string | null; images: string[];
}
export interface GoalEvaluation {
  number: number; revision: number; contract_digest: string;
  score: number | null; outcome: 'achieved' | 'not_achieved' | 'unknown';
  authority: 'cad' | 'mixed' | 'vision';
  per_requirement: GoalRequirementResult[]; evidence_ids: string[];
  passed_weight: number; total_weight: number; target_similarity: number; at: number;
}
export interface GoalState {
  version: 1; options: GoalOptions; phase: string;
  kind: 'cad' | 'informational' | null; contract: GoalContract | null;
  bindings: Record<string, Record<string, string>>; revision: number;
  document_identity: string | null; evaluations: GoalEvaluation[];
  best_passed_weight: number; no_progress: number;
  outcome: 'achieved' | 'not_achieved' | 'unknown' | null;
  completion_reason: string | null; last_score: number | null;
  authority: 'cad' | 'mixed' | 'vision' | null;
  model_turns: number; contract_errors: number; summary: string | null;
}
export interface GoalStateCompact {
  version: 1; phase: string | null;
  outcome: 'achieved' | 'not_achieved' | 'unknown' | null;
  completion_reason: string | null; last_score: number | null;
  authority: 'cad' | 'mixed' | 'vision' | null;
  best_passed_weight: number; no_progress: number; evaluation_count: number;
  max_evaluations: number; max_no_progress: number; target_similarity: number;
  summary: string | null;
}
export interface RunEvent {
  seq: number; type: string; at?: number; data: Record<string, unknown>;
  state?: Partial<Omit<Run, 'events' | 'messages' | 'goal'>> & {
    goal?: GoalStateCompact;
  };
}
export interface AppSettings {
  python_execution_allowed: boolean; vision_profile_id: string | null;
  vision_routing_approved: boolean; vision_routing_fingerprint: string | null;
}
export interface SaveResult {
  ok: boolean; saved?: string; conversation?: string; warning?: string;
}

export interface LoadedConversation {
  session_id: string; profile_id?: string; language?: string;
  document?: { path?: string }; runs: Run[];
}

export interface Run {
  id: string; session_id: string; profile_id: string;
  requested_profile_id?: string | null; language?: string; status: string;
  events: RunEvent[]; confirmation: Confirmation | null; call_count: number;
  max_calls: number; verification: string; stop_requested: boolean;
  repair_attempts?: number; repairing?: boolean; evaluation?: GoalOptions | null;
  goal?: GoalState | null;
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
