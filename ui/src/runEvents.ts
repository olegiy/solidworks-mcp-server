import type { Run, RunEvent } from './api';

export function applyRunEvent(run: Run, event: RunEvent): Run {
  const last = run.events.at(-1)?.seq ?? 0;
  if (!Number.isSafeInteger(event.seq) || event.seq < 1 || !event.type || !event.data) {
    throw new Error('Invalid run event');
  }
  if (event.seq <= last) return run;
  if (event.seq !== last + 1) throw new Error('Run event gap');
  return { ...run, ...event.state, events: [...run.events, event] };
}
