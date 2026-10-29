import type { GoalEvaluation, GoalRequirementResult, Run } from './api';
import { useI18n } from './i18n';
import type { MessageKey } from './i18n';

const phaseKeys: Record<string, MessageKey> = {
  preparing: 'goalStagePreparing', executing: 'goalStageExecuting',
  evaluating: 'goalStageEvaluating', correcting: 'goalStageCorrecting',
  finished: 'goalStageFinished',
};
const reasonKeys: Record<string, MessageKey> = {
  evaluation_limit: 'goalLimitEvaluations', no_progress: 'goalLimitNoProgress',
  call_limit: 'goalLimitCalls', model_turn_limit: 'goalLimitModelTurns',
  evaluation_unavailable: 'goalReasonEvidence', contract_missing: 'goalReasonContract',
  clarification_required: 'goalReasonClarification', stopped: 'statusStopped',
  rejected: 'statusRejected', uncertain: 'statusUncertain', failed: 'statusFailed',
};
const statusKeys: Record<string, MessageKey> = {
  pass: 'goalStatusPass', fail: 'goalStatusFail', unknown: 'goalStatusUnknown',
};
const sourceKeys: Record<string, MessageKey> = {
  cad: 'goalSourceCad', vision: 'goalSourceVision',
};

function formatNumber(value: number | null, unit: string | null, locale: string) {
  if (value === null) return '—';
  return `${new Intl.NumberFormat(locale, { maximumFractionDigits: 6 }).format(value)} ${unit ?? ''}`.trim();
}
function formatActual(item: GoalRequirementResult, locale: string) {
  const value = item.actual;
  if (value === null || value === undefined) return '—';
  return typeof value === 'number' ? formatNumber(value, item.unit, locale) : String(value);
}

export default function GoalEvaluationCard({ run }: { run: Run }) {
  const { language, t } = useI18n();
  const goal = run.goal;
  if (!goal) return null;
  const locale = language === 'ru' ? 'ru-RU' : 'en-US';
  const evaluations = goal.evaluations ?? [];
  const latest: GoalEvaluation | undefined = evaluations.at(-1);
  const informational = goal.kind === 'informational';
  const clarification = goal.completion_reason === 'clarification_required';
  const phase = phaseKeys[goal.phase] ? t(phaseKeys[goal.phase]) : goal.phase;
  const reason = goal.completion_reason && reasonKeys[goal.completion_reason]
    ? t(reasonKeys[goal.completion_reason]) : goal.completion_reason;
  const stalled = goal.outcome === null && ['stopped', 'rejected', 'uncertain', 'failed']
    .includes(goal.completion_reason ?? '');
  const outcome = informational && goal.completion_reason === 'informational'
      ? t('goalInformationalShort')
    : clarification ? t('goalClarification')
    : stalled && goal.completion_reason ? t(reasonKeys[goal.completion_reason] ?? 'goalUnknown')
    : goal.outcome === 'achieved' ? t('goalAchieved')
    : goal.outcome === 'not_achieved' ? t('goalNotAchieved')
    : goal.outcome === 'unknown' ? t('goalUnknown')
    : informational ? t('goalInformational') : phase;
  const warning = latest && (latest.authority === 'mixed' || latest.authority === 'vision')
    ? t('goalModelWarning')
    : latest?.per_requirement.some(item => item.source === 'vision') ? t('goalModelWarning')
    : null;
  const requirements = goal.contract?.requirements ?? [];
  const results = new Map((latest?.per_requirement ?? []).map(item => [item.requirement_id, item]));
  const maxEvaluations = goal.options?.max_evaluations ?? 10;
  const maxNoProgress = goal.options?.max_no_progress ?? 3;

  return <section className="goal-card panel" aria-label={t('goalTitle')} data-testid="goal-card">
    <header className="goal-heading">
      <div>
        <div className="eyebrow">{t('goalTitle')}</div>
        {latest?.score !== null && latest?.score !== undefined
          ? <p className="goal-score" aria-live="polite">{latest.score}%</p>
          : informational ? <p className="muted">{t('goalInformational')}</p> : null}
      </div>
      <div className="goal-state">
        <span className={`badge goal-${goal.outcome ?? 'pending'}`} aria-live="polite">{outcome}</span>
        <span className="muted">{t('goalStage')}: {phase}</span>
      </div>
    </header>
    {clarification && goal.contract?.ambiguities.length
      ? <p className="warning">{goal.contract.ambiguities.join(' · ')}</p>
      : null}
    {reason && goal.completion_reason && reasonKeys[goal.completion_reason]
      ? <p className={goal.outcome === 'achieved' ? 'notice' : 'warning'}>{reason}</p> : null}
    {goal.summary && ['evaluation_unavailable', 'contract_missing', 'stopped', 'rejected',
      'uncertain', 'failed'].includes(goal.completion_reason ?? '')
      ? <p className="hint">{goal.summary}</p> : null}
    {requirements.length ? <>
      <p className="hint">{t('goalFrozen')}</p>
      <div className="goal-table-wrap">
        <table className="goal-table">
          <caption className="sr-only">{t('goalRequirements')}</caption>
          <thead><tr>
            <th scope="col">{t('goalTitle')}</th>
            <th scope="col">{t('goalExpected')}</th>
            <th scope="col">{t('goalActual')}</th>
            <th scope="col">{t('goalColumnStatus')}</th>
            <th scope="col">{t('goalColumnSource')}</th>
          </tr></thead>
          <tbody>{requirements.map(requirement => {
            const result = results.get(requirement.id);
            const status = result?.status ?? 'unknown';
            const source = result?.source ? t(sourceKeys[result.source] ?? 'goalSourceNone') : t('goalSourceNone');
            return <tr key={requirement.id} data-testid={`goal-requirement-${requirement.id}`}>
              <th scope="row">
                <span>{requirement.description}</span>
                <small>{requirement.required ? t('goalRequired') : t('goalOptional')} · ×{requirement.weight} · {requirement.method === 'cad' ? t('goalSourceCad') : t('goalSourceVision')}</small>
                {result?.reason ? <small className="muted">{result.reason}</small> : null}
              </th>
              <td>{formatNumber(requirement.expected, requirement.unit, locale)}</td>
              <td>{result ? formatActual(result, locale) : '—'}</td>
              <td><span className={`goal-status ${status}`}>{t(statusKeys[status])}</span></td>
              <td>{source}</td>
            </tr>;
          })}</tbody>
        </table>
      </div>
    </> : null}
    <footer className="goal-footer">
      <span>{t('goalEvaluationCount', { current: evaluations.length, total: maxEvaluations })}</span>
      <span>{t('goalNoProgress', { count: goal.no_progress, total: maxNoProgress })}</span>
      <span>{t('goalBudget', { used: run.call_count, total: run.max_calls })}</span>
    </footer>
    {warning ? <p className="goal-warning">{warning}</p> : null}
  </section>;
}
