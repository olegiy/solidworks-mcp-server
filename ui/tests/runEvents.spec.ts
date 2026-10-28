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

test('construction updates retain checks, replay once, and preserve the previous plan', () => {
  const stages = [{ id: 'profile', title: 'Профиль', expected_result: '80 мм', status: 'done',
    tool_call_ids: ['py'], checks: [{ passed: true, expected: { kind: 'dimension' }, observed: [] }] }];
  const event = { seq: 2, type: 'construction_plan', data: { stages, history: [{ revision: 1 }] } };
  const next = applyRunEvent(run, event);
  expect(next.construction_plan).toEqual(stages);
  expect(next.construction_plan_history).toEqual([{ revision: 1 }]);
  expect(applyRunEvent(next, event)).toBe(next);
  expect(run.construction_plan).toBeUndefined();
});
