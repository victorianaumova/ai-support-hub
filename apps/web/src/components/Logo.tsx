/** The Tallybridge mark: two tally strokes joined by a bridge arch, in a rounded square. An
 * original drawing for this demo; the same shape is the favicon (public/favicon.svg). */
export function LogoMark({ size = 24 }: { size?: number }) {
  return (
    <svg aria-hidden="true" focusable="false" width={size} height={size} viewBox="0 0 24 24" className="shrink-0">
      <rect width="24" height="24" rx="6" fill="#1a73e8" />
      <path d="M5 17.5V11M19 17.5V11M5 11.5q7-7 14 0" fill="none" stroke="#ffffff" strokeWidth="2.25" strokeLinecap="round" />
      <path d="M9.5 17.5v-3.5M14.5 17.5v-3.5" fill="none" stroke="#ffffff" strokeWidth="2.25" strokeLinecap="round" />
    </svg>
  );
}

/** Mark + wordmark. `product` names the area (for example "Support console"). */
export function Logo({ product }: { product?: string }) {
  return (
    <span className="inline-flex items-center gap-2">
      <LogoMark />
      <span className="text-lg font-medium tracking-tight text-ink">Tallybridge</span>
      {product !== undefined && <span className="hidden text-lg text-muted sm:inline">{product}</span>}
    </span>
  );
}

/** The demo marker: a small badge, with "fictional company" for screen readers and as a tooltip. */
export function DemoBadge() {
  return (
    <span title="Demo · fictional company" className="chip chip-warn uppercase tracking-wider">
      <span aria-hidden="true">Demo</span>
      <span className="sr-only">Demo · fictional company</span>
    </span>
  );
}

/** The fictional-company line every public page shows once. */
export function FictionalNote() {
  return (
    <p className="text-xs text-muted">
      Tallybridge is a fictional payments company. Every customer, card and transaction here is made up.
    </p>
  );
}
