import { useEffect, useState } from 'react';
import { terminal } from './api';
import type { Run, RunEvent } from './api';
import { useI18n } from './i18n';
import type { MessageKey } from './i18n';
import { incompleteGoalKey, localTools } from './runPresentation';

export interface PendingActivity {
  phase: 'images' | 'sending'; at: number; files: number; bytes: number;
}
export interface RunContact {
  runId: string; at: number; failed: boolean;
}

const finishedKeys: Record<string, MessageKey> = {
  completed: 'activityCompleted', stopped: 'activityStopped', failed: 'activityFailed',
  uncertain: 'activityUncertain', limit_reached: 'activityLimit', rejected: 'activityRejected',
};
const toolKeys: Record<string, MessageKey> = {
  connect_solidworks: 'toolActivityConnectSolidworks', list_open_documents: 'toolActivityListDocuments',
  create_part: 'toolActivityCreatePart', create_assembly: 'toolActivityCreateAssembly',
  create_sketch: 'toolActivityCreateSketch', close_sketch: 'toolActivityCloseSketch',
  sketch_line: 'toolActivitySketchLine', sketch_circle: 'toolActivitySketchCircle',
  sketch_rectangle: 'toolActivitySketchRectangle', create_base_flange: 'toolActivityBaseFlange',
  cut_through: 'toolActivityCutThrough', rebuild: 'toolActivityRebuild',
  set_dimension: 'toolActivitySetDimension', get_dimension: 'toolActivityGetDimension',
  get_feature_tree: 'toolActivityGetFeatureTree', get_mass_properties: 'toolActivityMassProperties',
  get_sheet_metal_info: 'toolActivitySheetMetal', screenshot: 'toolActivityScreenshot',
  execute_python: 'toolActivityExecutePython', save_document: 'toolActivitySave',
  export_flat_pattern_dxf: 'toolActivityExportDxf', get_components: 'toolActivityComponents',
  add_component: 'toolActivityAddComponent', add_mate: 'toolActivityAddMate',
};

type Translate = ReturnType<typeof useI18n>['t'];

function stamp(event?: RunEvent) {
  return event?.at !== undefined && Number.isFinite(event.at) ? event.at * 1000 : undefined;
}
function duration(start: number | undefined, end: number) {
  if (start === undefined) return '—';
  const seconds = Math.max(0, Math.floor((end - start) / 1000));
  const parts = [Math.floor(seconds / 60) % 60, seconds % 60];
  if (seconds >= 3600) parts.unshift(Math.floor(seconds / 3600));
  return parts.map(value => String(value).padStart(2, '0')).join(':');
}
function stageOf(run: Run, t: Translate) {
  const latestFirst = [...run.events].reverse();
  const status = latestFirst.find(event => event.type === 'status' && event.data.status === run.status);
  if (terminal(run.status)) {
    const key = incompleteGoalKey(run) ?? finishedKeys[run.status];
    return { label: key ? t(key) : run.status, at: stamp(status), kind: 'finished' };
  }
  if (run.status === 'stopping') {
    return { label: t('activityStopping'), at: stamp(status), kind: 'stopping' };
  }
  if (run.status === 'awaiting_confirmation') {
    return { label: t('activityConfirmation'), at: stamp(status), kind: 'confirmation' };
  }
  if (run.status === 'repairing') {
    return { label: t('activityRepair', { attempt: run.repair_attempts ?? 1, total: 2 }),
      at: stamp(status), kind: 'repair' };
  }
  const latest = latestFirst.find(event => ['model_start', 'model_end', 'tool_start', 'tool_result', 'status'].includes(event.type));
  if (latest?.type === 'model_start') return { label: t('activityWaitingModel'), at: stamp(latest), kind: 'model' };
  if (latest?.type === 'model_end') return { label: t('activityProcessingResponse'), at: stamp(latest), kind: 'response' };
  if (latest?.type === 'tool_start') {
    const name = String(latest.data.name);
    const action = toolKeys[name];
    if (localTools.has(name)) return { label: t('activityProcessingTool'), at: stamp(latest), kind: 'local' };
    return { label: `${latest.data.verification ? t('activityChecking') : t('activitySolidworks')}: ${action ? t(action) : name}`,
      at: stamp(latest), kind: 'cad' };
  }
  if (run.goal && run.status === 'running' && run.goal.phase !== 'finished') {
    const key = run.goal.phase === 'evaluating' ? 'goalStageEvaluating'
      : run.goal.phase === 'correcting' ? 'goalStageCorrecting'
      : run.goal.phase === 'executing' ? 'goalStageExecuting' : 'goalStagePreparing';
    return { label: run.goal.phase === 'evaluating'
      ? `${t('activityChecking')} / ${t(key)}` : t(key), at: stamp(status), kind: 'goal' };
  }
  return { label: latest?.type === 'tool_result' ? t('activityProcessingTool') : t('activityPreparing'),
    at: stamp(latest ?? run.events[0]), kind: 'preparing' };
}

export default function ActivityIndicator({ run, pending, contact }: {
  run?: Run; pending: PendingActivity | null; contact: RunContact | null;
}) {
  const { language, t } = useI18n();
  const [now, setNow] = useState(Date.now);
  const active = !!pending || (!!run && !terminal(run.status));
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  if (!run && !pending) return null;

  const stage = pending
    ? { label: pending.phase === 'images' ? t('activityImages') : t('activitySending'), at: pending.at, kind: pending.phase }
    : stageOf(run!, t);
  const lastEvent = run?.events.at(-1);
  const firstAt = pending?.at ?? stamp(run?.events[0]);
  const endedAt = !pending && run && terminal(run.status) ? stage.at : undefined;
  const end = endedAt ?? now;
  const observed = !pending && run && contact?.runId === run.id ? contact : null;
  const disconnected = active && !pending && (!observed || observed.failed || now - observed.at >= 8000);
  const longWait = stage.kind === 'model' && stage.at !== undefined && now - stage.at >= 30000;
  const label = disconnected ? t('activityDisconnected') : longWait ? t('activityLongWait') : stage.label;
  const pulse = active && !disconnected && stage.kind !== 'confirmation';
  const modelStarts = run?.events.filter(event => event.type === 'model_start').length ?? 0;
  const modelReplies = run?.events.filter(event => event.type === 'model_end' && event.data.outcome === 'received').length ?? 0;
  const cadStarts = new Set(run?.events.filter(event => event.type === 'tool_start'
    && !localTools.has(String(event.data.name))).map(event => event.data.tool_call_id));
  const cadResults = new Set(run?.events.filter(event => event.type === 'tool_result' && cadStarts.has(event.data.tool_call_id))
    .map(event => event.data.tool_call_id));
  const elapsed = endedAt !== undefined ? duration(firstAt, endedAt) : duration(stage.at, now);

  return <section className={`activity-indicator${disconnected ? ' disconnected' : ''}`}
    role="region" aria-label={t('activityRegion')} data-running={pulse}>
    <details key={pending ? `pending-${pending.at}` : run?.id}>
      <summary role="button" aria-label={`${label}. ${t('activityDetails')}`}>
        <span className="activity-dot" aria-hidden="true" />
        <span className="activity-stage" role="status" aria-live="polite" aria-atomic="true">{label}</span>
        <span className="activity-time" aria-label={t('activityClock')} aria-live="off">{elapsed}</span>
        <span className="activity-chevron" aria-hidden="true">⌄</span>
      </summary>
      <div className="activity-details">
        {disconnected ? <p className="warning">{t('activityDisconnectedDetails', { stage: stage.label })}</p> : null}
        {stage.kind === 'stopping' ? <p>{t('activityStoppingDetails')}</p> : null}
        <p>{t('activityTotalTime', { time: duration(firstAt, end),
          missing: firstAt === undefined ? t('activityTimeNotRecorded') : '' })}</p>
        {pending ? <p>{t('activityImagesCount', { count: pending.files, size: new Intl.NumberFormat(
          language === 'ru' ? 'ru-RU' : 'en-US', { maximumFractionDigits: 1 }).format(pending.bytes / 1024) })}</p> : <>
          <p>{t('activityModelCalls', { calls: modelStarts, replies: modelReplies })}</p>
          <p>{t('activityCadDone', { done: cadResults.size, total: cadStarts.size })}</p>
          {active ? <p>{t('activityLastEvent', { time: duration(stamp(lastEvent), now) })}
            {!disconnected ? t('activityAppResponds') : ''}</p> : null}
        </>}
      </div>
    </details>
  </section>;
}
