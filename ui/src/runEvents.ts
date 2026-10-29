import type { GoalContract, GoalEvaluation, Run, RunEvent } from './api';

export function applyRunEvent(run: Run, event: RunEvent): Run {
  const last = run.events.at(-1)?.seq ?? 0;
  if (!Number.isSafeInteger(event.seq) || event.seq < 1 || !event.type || !event.data) {
    throw new Error('Invalid run event');
  }
  if (event.seq <= last) return run;
  if (event.seq !== last + 1) throw new Error('Run event gap');
  const { goal: compactStateGoal, ...state } = event.state ?? {};
  const next: Run = { ...run, ...state, events: [...run.events, event] };
  if (next.goal) {
    let goal = next.goal;
    if (compactStateGoal) {
      const compact = compactStateGoal;
      goal = {
        ...goal,
        phase: compact.phase ?? goal.phase,
        outcome: compact.outcome ?? goal.outcome,
        completion_reason: compact.completion_reason ?? goal.completion_reason,
        last_score: compact.last_score ?? goal.last_score,
        authority: compact.authority ?? goal.authority,
        best_passed_weight: compact.best_passed_weight ?? goal.best_passed_weight,
        no_progress: compact.no_progress ?? goal.no_progress,
        summary: compact.summary ?? goal.summary,
        options: {
          ...goal.options,
          max_evaluations: compact.max_evaluations ?? goal.options.max_evaluations,
          max_no_progress: compact.max_no_progress ?? goal.options.max_no_progress,
          target_similarity: compact.target_similarity ?? goal.options.target_similarity,
        },
      };
    }
    const data = event.data;
    if (event.type === 'goal_defined' && data.contract) {
      const contract = data.contract as GoalContract;
      goal = { ...goal, contract, kind: contract.kind };
    } else if (event.type === 'goal_bindings' && data.bindings) {
      goal = { ...goal, bindings: data.bindings as Record<string, Record<string, string>> };
    } else if (event.type === 'goal_revision' && Number.isSafeInteger(data.revision)) {
      goal = { ...goal, revision: data.revision as number };
    } else if (event.type === 'goal_evaluation' && (data.evaluation || data.requirements)) {
      const evaluation = data.evaluation
        ? data.evaluation as GoalEvaluation
        : {
            number: Number(data.number),
            revision: Number(data.revision),
            contract_digest: goal.contract?.contract_digest ?? '',
            score: data.score as number | null,
            outcome: data.outcome as GoalEvaluation['outcome'],
            authority: data.authority as GoalEvaluation['authority'],
            per_requirement: (data.requirements as Array<Record<string, unknown>>).map(item => {
              const requirement = goal.contract?.requirements.find(value => value.id === item.id);
              return {
                requirement_id: item.id as string, description: requirement?.description ?? '',
                required: requirement?.required ?? true, weight: requirement?.weight ?? 1,
                method: requirement?.method ?? 'cad',
                status: item.status as GoalEvaluation['per_requirement'][number]['status'],
                actual: item.actual as number | string | null,
                expected: item.expected as number | null,
                tolerance: requirement?.tolerance ?? null,
                unit: item.unit as 'mm' | 'deg' | null,
                source: item.source as 'cad' | 'vision' | null, reason: '',
                evidence_id: null, images: [],
              };
            }),
            evidence_ids: [], passed_weight: Number(data.passed_weight),
            total_weight: Number(data.total_weight),
            target_similarity: goal.options.target_similarity, at: 0,
          };
      const evaluations = [...goal.evaluations.filter(item => item.number !== evaluation.number), evaluation]
        .sort((left, right) => left.number - right.number);
      goal = {
        ...goal, evaluations,
        last_score: evaluation.score,
        outcome: evaluation.outcome,
        authority: evaluation.authority,
      };
    } else if (event.type === 'goal_finished') {
      goal = {
        ...goal,
        summary: typeof data.summary === 'string' ? data.summary : goal.summary,
      };
    }
    next.goal = goal;
  }
  return next;
}
