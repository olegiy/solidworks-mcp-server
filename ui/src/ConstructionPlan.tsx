import type { ConstructionStage, Run } from './api';
import { useI18n } from './i18n';

const labels = {
  ru: { title: 'Этапы построения', pending: 'Ожидает', running: 'Выполняется', done: 'Завершён',
    error: 'Ошибка', stopped: 'Остановлен', code: 'Код этапа', evidence: 'Доказательства',
    passed: 'Проверка пройдена', failed: 'Проверка не пройдена',
    noChecks: 'Свойства этапа не проверены' },
  en: { title: 'Construction stages', pending: 'Pending', running: 'Running', done: 'Done',
    error: 'Error', stopped: 'Stopped', code: 'Stage code', evidence: 'Evidence',
    passed: 'Check passed', failed: 'Check failed', noChecks: 'Stage properties have not been checked' },
};

export default function ConstructionPlan({ run }: { run: Run }) {
  const { language } = useI18n();
  const text = labels[language];
  const stages = run.construction_plan ?? [];
  if (!stages.length) return null;
  const codeByCall = new Map<string, string>();
  for (const event of run.events) {
    if (event.type === 'tool_start' && event.data.name === 'execute_python') {
      const args = event.data.arguments as { code?: string } | undefined;
      if (args?.code) codeByCall.set(String(event.data.tool_call_id), args.code);
    }
  }
  return <section className="construction-plan" role="region" aria-label={text.title}>
    <h3>{text.title}</h3>
    <ol>{stages.map((stage: ConstructionStage) => <li key={stage.id}>
      <div className="construction-heading"><strong>{stage.title}</strong>
        <span className={`badge ${stage.status}`} role="status">{text[stage.status]}</span></div>
      <p className="muted">{stage.expected_result}</p>
      {stage.checks.length ? <ul className="construction-checks">{stage.checks.map((check, index) =>
        <li key={index}><span className={check.passed ? 'check-passed' : 'error'}>
          {check.passed ? text.passed : text.failed}</span>
          <details><summary>{text.evidence}</summary>
            <pre tabIndex={0}>{JSON.stringify(check, null, 2)}</pre></details></li>)}</ul>
        : stage.status === 'done' ? <p className="hint">{text.noChecks}</p> : null}
      {stage.tool_call_ids.filter(id => codeByCall.has(id)).map(id =>
        <details key={id}><summary>{text.code}</summary>
          <pre tabIndex={0}>{codeByCall.get(id)}</pre></details>)}
    </li>)}</ol>
  </section>;
}
