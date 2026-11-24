/** The Tallybridge mark: two tally strokes joined by a bridge arch, in a dark rounded square. An
 * original drawing for this demo; the same shape is the favicon (public/favicon.svg). */
export function LogoMark({ size = 32 }: { size?: number }) {
  return (
    <svg aria-hidden="true" focusable="false" width={size} height={size} viewBox="0 0 24 24" className="shrink-0">
      <rect width="24" height="24" rx="7" fill="#17161d" />
      <path d="M5.5 17V11.5M18.5 17V11.5M5.5 12q6.5-6.5 13 0" fill="none" stroke="#ffffff" strokeWidth="2.25" strokeLinecap="round" />
      <path d="M9.75 17v-3.25M14.25 17v-3.25" fill="none" stroke="#ffffff" strokeWidth="2.25" strokeLinecap="round" />
    </svg>
  );
}

/** Mark + wordmark. `product` names the area (for example "Support Hub"). */
export function Logo({ product }: { product?: string }) {
  return (
    <span className="inline-flex items-center gap-2.5">
      <LogoMark />
      <span className="text-[1.375rem] font-medium tracking-[-0.02em] text-ink">tallybridge</span>
      {product !== undefined && <span className="hidden text-[1.375rem] tracking-[-0.02em] text-muted sm:inline">{product}</span>}
    </span>
  );
}

/** The demo marker: a small badge, with "fictional company" for screen readers and as a tooltip. */
export function DemoBadge() {
  return (
    <span title="Demo · fictional company" className="inline-flex h-6 items-center rounded-full bg-white px-2.5 text-xs font-medium text-muted shadow-lift">
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
