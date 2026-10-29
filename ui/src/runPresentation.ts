import type { Run } from './api';
import type { MessageKey } from './i18n';

export const localTools = new Set([
  'cadnext_define_goal', 'cadnext_bind_requirements', 'cadnext_report_visual_evaluation',
  'set_construction_plan',
]);

export function incompleteGoalKey(run: Run): MessageKey | null {
  const goal = run.goal;
  if (run.status !== 'completed' || !goal) return null;
  if (goal.completion_reason === 'clarification_required') return 'goalClarification';
  if (goal.kind === 'informational' || goal.outcome === 'achieved') return null;
  return goal.outcome === 'not_achieved' ? 'goalNotAchieved' : 'goalUnknown';
}
