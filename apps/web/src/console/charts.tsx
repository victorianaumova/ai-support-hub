/**
 * Small accessible charts (UI §4): every chart is drawn with plain elements and comes with its
 * numbers as a table, so colour is never the only signal and screen readers get the data. Bar
 * widths are set through React's `style` (CSSOM), which the API's CSP allows.
 */
export interface Bar {
  label: string;
  value: number;
  shown?: string;
}

export function BarList({ title, bars }: { title: string; bars: Bar[] }) {
  const max = Math.max(1, ...bars.map((b) => b.value));
  return (
    <figure aria-label={title} className="rounded-lg border border-slate-200 bg-white p-4">
      <figcaption className="font-semibold">{title}</figcaption>
      {bars.length === 0 ? (
        <p className="mt-2 text-sm text-muted">No data in this period.</p>
      ) : (
        <table className="mt-2 w-full text-sm">
          <tbody>
            {bars.map((bar) => (
              <tr key={bar.label}>
                <th scope="row" className="w-1/3 py-1 pr-2 text-left font-normal">
                  {bar.label}
                </th>
                <td className="py-1">
                  <div className="flex items-center gap-2">
                    <div aria-hidden="true" className="h-3 rounded bg-brand-600" style={{ width: `${(bar.value / max) * 100}%` }} />
                    <span className="tabular-nums">{bar.shown ?? bar.value}</span>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </figure>
  );
}

export interface Series {
  key: string;
  label: string;
  /** Tailwind classes; each series also has a pattern letter in the legend. */
  className: string;
}

/** Stacked bars per day with a legend and the table of numbers. */
export function StackedDays({ title, series, points }: { title: string; series: Series[]; points: { date: string; values: Record<string, number | null> }[] }) {
  const max = Math.max(1, ...points.map((p) => series.reduce((sum, s) => sum + (p.values[s.key] ?? 0), 0)));
  return (
    <figure aria-label={title} className="rounded-lg border border-slate-200 bg-white p-4">
      <figcaption className="font-semibold">{title}</figcaption>
      <ul className="mt-2 flex flex-wrap gap-3 text-xs">
        {series.map((s) => (
          <li key={s.key} className="flex items-center gap-1">
            <span aria-hidden="true" className={`inline-block size-3 rounded-sm ${s.className}`} />
            {s.label}
          </li>
        ))}
      </ul>
      <div aria-hidden="true" className="mt-3 flex h-32 items-end gap-1">
        {points.map((p) => (
          <div key={p.date} className="flex h-full flex-1 flex-col-reverse" title={p.date}>
            {series.map((s) => (
              <div key={s.key} className={s.className} style={{ height: `${((p.values[s.key] ?? 0) / max) * 100}%` }} />
            ))}
          </div>
        ))}
      </div>
      <details className="mt-2 text-sm">
        <summary className="cursor-pointer text-muted">Numbers</summary>
        <table className="mt-1 w-full">
          <thead>
            <tr>
              <th scope="col" className="text-left">Day</th>
              {series.map((s) => <th key={s.key} scope="col" className="text-right">{s.label}</th>)}
            </tr>
          </thead>
          <tbody>
            {points.map((p) => (
              <tr key={p.date}>
                <th scope="row" className="text-left font-normal">{p.date}</th>
                {series.map((s) => <td key={s.key} className="text-right tabular-nums">{p.values[s.key] ?? "—"}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </figure>
  );
}
