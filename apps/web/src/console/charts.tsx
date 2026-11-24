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
    <figure aria-label={title} className="panel p-6">
      <figcaption className="text-lg font-medium">{title}</figcaption>
      {bars.length === 0 ? (
        <p className="mt-3 text-muted">No data in this period.</p>
      ) : (
        <table className="mt-4 w-full">
          <tbody>
            {bars.map((bar) => (
              <tr key={bar.label}>
                <th scope="row" className="w-1/3 py-1.5 pr-3 text-left font-normal text-[var(--tb-text-2)]">
                  {bar.label}
                </th>
                <td className="py-1.5">
                  <div className="flex items-center gap-3">
                    <div aria-hidden="true" className="hatch h-7 flex-1 overflow-hidden rounded-full">
                      <div className="h-full rounded-full bg-accent transition-[width] duration-300" style={{ width: `${(bar.value / max) * 100}%` }} />
                    </div>
                    <span className="w-24 shrink-0 tabular-nums">{bar.shown ?? bar.value}</span>
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
    <figure aria-label={title} className="panel p-6">
      <figcaption className="text-lg font-medium">{title}</figcaption>
      <ul className="mt-3 flex flex-wrap gap-4 text-xs text-[var(--tb-text-2)]">
        {series.map((s) => (
          <li key={s.key} className="flex items-center gap-1.5">
            <span aria-hidden="true" className={`inline-block size-2.5 rounded-full ${s.className}`} />
            {s.label}
          </li>
        ))}
      </ul>
      <div aria-hidden="true" className="mt-4 flex h-40 items-end gap-2">
        {points.map((p) => (
          <div key={p.date} className="hatch flex h-full flex-1 flex-col-reverse overflow-hidden rounded-xl" title={p.date}>
            {series.map((s) => (
              <div key={s.key} className={`${s.className} last:rounded-t-xl`} style={{ height: `${((p.values[s.key] ?? 0) / max) * 100}%` }} />
            ))}
          </div>
        ))}
      </div>
      <div aria-hidden="true" className="mt-2 flex gap-2 text-xs text-muted">
        {points.map((p) => <span key={p.date} className="flex-1 truncate text-center">{p.date.slice(5)}</span>)}
      </div>
      <details className="mt-3">
        <summary className="cursor-pointer text-muted hover:text-ink">Numbers</summary>
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
