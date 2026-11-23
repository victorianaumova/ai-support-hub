/**
 * Waiting time against the SLA (UI §3; world file §8; `support_hours` in config/policy.yaml,
 * which `sla.test.ts` compares with the constants below). Urgent tickets: 30 minutes when the
 * wait starts in support hours, 60 outside them (the on-call analyst); standard ones: 4 business
 * hours. Support hours: Monday–Friday 08:00–20:00 and Saturday 10:00–16:00, Europe/Bratislava.
 */
export const ZONE = "Europe/Bratislava";
export const WEEKDAYS = { open: 8 * 60, close: 20 * 60 };
export const SATURDAY = { open: 10 * 60, close: 16 * 60 };
export const URGENT_MINUTES = 30;
export const URGENT_OUT_OF_HOURS_MINUTES = 60;
export const STANDARD_BUSINESS_MINUTES = 4 * 60;
// Urgent as the policy says (world file §8: fraud, lost card, restricted account): triage urgency
// `high`, or one of these route reasons or intents.
export const URGENT_REASONS: readonly string[] = ["fraud_report", "restricted_account"];
export const URGENT_INTENTS: readonly string[] = ["card_lost_stolen", "account_restricted"];
const STEP_MS = 5 * 60_000;

const parts = new Intl.DateTimeFormat("en-GB", { timeZone: ZONE, weekday: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });

export function isUrgent(ticket: { urgency: string | null; intent: string | null; route_reasons: readonly string[] }): boolean {
  return (
    ticket.urgency === "high" ||
    (ticket.intent !== null && URGENT_INTENTS.includes(ticket.intent)) ||
    ticket.route_reasons.some((reason) => URGENT_REASONS.includes(reason))
  );
}

export function isOpen(at: Date): boolean {
  const found = Object.fromEntries(parts.formatToParts(at).map((p) => [p.type, p.value]));
  const minutes = Number(found.hour) * 60 + Number(found.minute);
  if (found.weekday === "Sun") return false;
  const hours = found.weekday === "Sat" ? SATURDAY : WEEKDAYS;
  return minutes >= hours.open && minutes < hours.close;
}

/** Business minutes between two times, in 5-minute steps (enough for a red mark). */
export function businessMinutes(from: Date, to: Date): number {
  let total = 0;
  // A week of waiting is late in any case; stop counting there.
  const end = Math.min(to.getTime(), from.getTime() + 7 * 24 * 3_600_000);
  for (let t = from.getTime(); t < end; t += STEP_MS) {
    if (isOpen(new Date(t))) total += 5;
  }
  return total;
}

export function isOverdue(waitingSince: string, urgent: boolean, now: Date): boolean {
  const since = new Date(waitingSince);
  if (urgent) {
    const limit = isOpen(since) ? URGENT_MINUTES : URGENT_OUT_OF_HOURS_MINUTES;
    return now.getTime() - since.getTime() > limit * 60_000;
  }
  return businessMinutes(since, now) > STANDARD_BUSINESS_MINUTES;
}

export function waited(waitingSince: string, now: Date): string {
  const minutes = Math.max(0, Math.round((now.getTime() - new Date(waitingSince).getTime()) / 60_000));
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours} h ${minutes % 60} min`;
  return `${Math.floor(hours / 24)} days`;
}
