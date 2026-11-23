/**
 * Waiting time against the SLA (UI §3; world file §8, `support_hours` in config/policy.yaml):
 * urgent tickets 30 minutes, standard ones 4 business hours. Business hours are Monday–Friday
 * 08:00–20:00 and Saturday 10:00–16:00 in Europe/Bratislava. Keep in step with policy.yaml.
 */
export const URGENT_MINUTES = 30;
export const STANDARD_BUSINESS_MINUTES = 4 * 60;
const ZONE = "Europe/Bratislava";
const STEP_MS = 5 * 60_000;

const parts = new Intl.DateTimeFormat("en-GB", { timeZone: ZONE, weekday: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });

function isOpen(at: Date): boolean {
  const found = Object.fromEntries(parts.formatToParts(at).map((p) => [p.type, p.value]));
  const minutes = Number(found.hour) * 60 + Number(found.minute);
  if (found.weekday === "Sun") return false;
  if (found.weekday === "Sat") return minutes >= 600 && minutes < 960;
  return minutes >= 480 && minutes < 1200;
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
  if (urgent) return now.getTime() - since.getTime() > URGENT_MINUTES * 60_000;
  return businessMinutes(since, now) > STANDARD_BUSINESS_MINUTES;
}

export function waited(waitingSince: string, now: Date): string {
  const minutes = Math.max(0, Math.round((now.getTime() - new Date(waitingSince).getTime()) / 60_000));
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours} h ${minutes % 60} min`;
  return `${Math.floor(hours / 24)} days`;
}
