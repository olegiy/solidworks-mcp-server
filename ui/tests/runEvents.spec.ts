import { expect, test } from '@playwright/test';
import { applyRunEvent } from '../src/runEvents';
import type { Run } from '../src/api';

const run: Run = { id: 'r', session_id: 's', profile_id: 'p', status: 'running',
  confirmation: null, call_count: 0, max_calls: 80, verification: 'not_required',
  stop_requested: false, events: [{ seq: 1, type: 'model_start', data: { number: 1 } }] };

test('applies event state and images once without altering earlier snapshots', () => {
  const event = { seq: 2, type: 'image', at: 1, data: { mimeType: 'image/png', data: 'picture' },
    state: { call_count: 1, verification: 'unverified' } };
  const next = applyRunEvent(run, event);
  expect(next.call_count).toBe(1);
  expect(next.verification).toBe('unverified');
  expect(next.events.map(item => item.type)).toEqual(['model_start', 'image']);
  expect(applyRunEvent(next, event)).toBe(next);
  expect(run.events).toHaveLength(1);
  expect(run.call_count).toBe(0);
});

test('a missing event requires snapshot recovery instead of skipping history', () => {
  expect(() => applyRunEvent(run, { seq: 3, type: 'status', data: {},
    state: { status: 'completed' } })).toThrow('gap');
  expect(run.status).toBe('running');
});

test('compact goal state merges without dropping the full evaluations', () => {
  const goalRun: Run = { ...run, goal: {
    version: 1, options: { enabled: true, target_similarity: 100, max_evaluations: 10,
      max_no_progress: 3 }, phase: 'evaluating', kind: 'cad', contract: null,
    bindings: {}, revision: 0, document_identity: null,
    evaluations: [{ number: 1, revision: 0, contract_digest: 'd', score: 70,
      outcome: 'not_achieved', authority: 'cad', per_requirement: [], evidence_ids: [],
      passed_weight: 7, total_weight: 10, target_similarity: 100, at: 1 }],
    best_passed_weight: 7, no_progress: 0, outcome: 'not_achieved',
    completion_reason: null, last_score: 70, authority: 'cad', model_turns: 2,
    contract_errors: 0, summary: null } };
  const next = applyRunEvent(goalRun, { seq: 2, type: 'goal_evaluation', data: {},
    state: { status: 'running', goal: { version: 1, phase: 'correcting',
      outcome: 'not_achieved', completion_reason: null, last_score: 70,
      authority: 'cad', best_passed_weight: 7, no_progress: 1,
      evaluation_count: 1, max_evaluations: 10, max_no_progress: 3,
      target_similarity: 100, summary: null } } });
  expect(next.goal?.phase).toBe('correcting');
  expect(next.goal?.no_progress).toBe(1);
  expect(next.goal?.evaluations).toHaveLength(1);
  expect(next.goal?.options.max_evaluations).toBe(10);
});

test('goal events publish frozen requirements and evaluations before completion', () => {
  const goalRun: Run = { ...run, goal: {
    version: 1, options: { enabled: true, target_similarity: 100, max_evaluations: 10,
      max_no_progress: 3 }, phase: 'preparing', kind: null, contract: null,
    bindings: {}, revision: 0, document_identity: null, evaluations: [],
    best_passed_weight: 0, no_progress: 0, outcome: null,
    completion_reason: null, last_score: null, authority: null, model_turns: 0,
    contract_errors: 0, summary: null } };
  const contract = { version: 1 as const, kind: 'cad' as const, reference_ids: [],
    ambiguities: [], contract_digest: 'd', requirements: [{ id: 'd1',
      description: 'Ширина', source: 'request' as const, required: true, weight: 1,
      method: 'cad' as const, expected: 50, tolerance: 0.01, unit: 'mm' as const }] };
  const defined = applyRunEvent(goalRun, { seq: 2, type: 'goal_defined',
    data: { contract, requirements_count: 1 }, state: { status: 'running' } });
  expect(defined.goal?.contract?.requirements[0].id).toBe('d1');
  expect(defined.goal?.kind).toBe('cad');
  const evaluated = applyRunEvent(defined, { seq: 3, type: 'goal_evaluation',
    data: { number: 1, revision: 1, score: 0, outcome: 'not_achieved', authority: 'cad',
      passed_weight: 0, total_weight: 1, requirements: [{ id: 'd1', status: 'fail',
        actual: 40, expected: 50, unit: 'mm', source: 'cad' }] },
    state: { status: 'running', goal: { version: 1,
      phase: 'evaluating', outcome: 'not_achieved', completion_reason: null,
      last_score: 0, authority: 'cad', best_passed_weight: 0, no_progress: 0,
      evaluation_count: 1, max_evaluations: 10, max_no_progress: 3,
      target_similarity: 100, summary: null } } });
  expect(evaluated.goal?.evaluations).toHaveLength(1);
  expect(evaluated.goal?.evaluations[0].per_requirement[0].status).toBe('fail');
  expect(evaluated.goal?.last_score).toBe(0);
});
