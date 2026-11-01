import type { MeshVerificationData } from './api';

const reasons: Record<string, string> = {
  restored_history: 'Сохранённые доказательства исторические. Для продолжения нужна новая проверка CAD.',
  not_compared: 'Сравнение ещё не выполнено.', cad_changed: 'CAD изменён; нужна новая проверка.',
  cad_export_unavailable: 'Экспорт CAD-сетки недоступен.', cad_export_failed: 'Не удалось получить CAD-сетку.',
  comparison_unavailable: 'Сравнение недоступно.', stale_cad_snapshot: 'Документ или ревизия CAD изменились.',
  unknown_or_insufficient_cad_precision: 'Точность CAD-триангуляции не подтверждена.',
  cad_checks_unconfirmed: 'Независимые CAD-проверки не подтверждены.',
  reference_not_closed_or_oriented: 'Исходная сетка открыта или имеет несогласованный обход.',
  cad_not_closed_or_oriented: 'Проверочная CAD-сетка открыта или имеет несогласованный обход.',
  region_limit: 'Превышен предел отдельных областей.', unconfirmed_regions: 'Обязательные области не проверены.',
  incomplete_triangle_coverage: 'Не все исходные треугольники проверены.',
  incomplete_evidence: 'Полнота доказательств не подтверждена.',
};
export default function MeshVerification({ value }: { value?: MeshVerificationData | null }) {
  if (!value) return null;
  const comparison = value.comparison;
  return <section className="mesh-verification panel" role="region" aria-label="Соответствие сетке">
    <strong>Соответствие сетке: {value.status === 'pass' ? 'Подтверждено по выборке'
      : value.status === 'fail' ? 'Есть расхождения' : 'Не подтверждено'}</strong>
    {value.reasons.map(reason => <p key={reason}>{reasons[reason] ?? 'Недостаточно оснований для проверки.'}</p>)}
    {comparison ? <>
      <p>Допуск {comparison.tolerance_mm} мм; погрешность триангуляции {comparison.chord_tolerance_mm} мм.</p>
      {comparison.coverage ? <div aria-label="Покрытие геометрии">
        {(['reference_to_cad', 'cad_to_reference'] as const).map(direction => {
          const coverage = comparison.coverage![direction];
          return <p key={direction}>{direction === 'reference_to_cad' ? 'Исходник' : 'CAD'}: проверено{' '}
            {coverage.checked_faces} из {coverage.total_faces} граней, участков {coverage.partitions}.{' '}
            {coverage.complete ? 'Покрытие полное.' : 'Покрытие неполное.'}</p>;
        })}
        {comparison.required_regions?.length ? <p>Обязательные области: проверено{' '}
          {comparison.required_regions.filter(region => region.complete && region.samples === 256).length}{' '}
          из {comparison.required_regions.length}.</p> : null}
      </div> : null}
      <table><thead><tr><th>Направление</th><th>RMS, мм</th><th>p95, мм</th><th>Выборочный максимум, мм</th></tr></thead>
        <tbody>{([['Исходник → CAD', comparison.reference_to_cad], ['CAD → исходник', comparison.cad_to_reference]] as const)
          .map(([label, stats]) => <tr key={label}><td>{label}</td><td>{stats.rms_mm.toPrecision(4)}</td>
            <td>{stats.p95_mm.toPrecision(4)}</td><td>{stats.sampled_max_mm.toPrecision(4)}</td></tr>)}</tbody></table>
      <p>Проверка по выборке не гарантирует максимум отклонения всей поверхности.</p>
    </> : null}
  </section>;
}
