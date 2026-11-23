/// <reference types="node" />
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import {
  businessMinutes,
  isOpen,
  isOverdue,
  isUrgent,
  SATURDAY,
  STANDARD_BUSINESS_MINUTES,
  URGENT_INTENTS,
  URGENT_MINUTES,
  URGENT_OUT_OF_HOURS_MINUTES,
  URGENT_REASONS,
  waited,
  WEEKDAYS,
  ZONE,
} from "./sla";

// Read from disk in the test only: the browser bundle never contains the policy file.
// Vitest runs in apps/web (jsdom gives `import.meta.url` an http scheme).
const policy = readFileSync(resolve(process.cwd(), "../../config/policy.yaml"), "utf8");

/** The `support_hours` block of config/policy.yaml, read as text (one value per line). */
function supportHours(): Record<string, string> {
  const block = policy.split(/^support_hours:\n/m)[1]?.split(/^\S/m)[0] ?? "";
  const values: Record<string, string> = {};
  for (const line of block.split("\n")) {
    const match = /^ {2}(\w+):\s*(.+)$/.exec(line);
    if (match?.[1] !== undefined && match[2] !== undefined) values[match[1]] = match[2].trim();
  }
  return values;
}

const minutes = (hhmm: string) => {
  const [h = "0", m = "0"] = hhmm.split(":");
  return Number(h) * 60 + Number(m);
};
const range = (value: string | undefined) => {
  const [open = "", close = ""] = (value ?? "").replaceAll('"', "").split("-");
  return { open: minutes(open), close: minutes(close) };
};
const list = (value: string | undefined) => (value ?? "").replace(/[[\]]/g, "").split(",").map((item) => item.trim()).filter(Boolean);

describe("SLA constants match config/policy.yaml", () => {
  const hours = supportHours();

  it("support hours and time zone", () => {
    expect(hours.timezone).toBe(ZONE);
    expect(range(hours.weekdays)).toEqual(WEEKDAYS);
    expect(range(hours.saturday)).toEqual(SATURDAY);
  });

  it("SLA minutes", () => {
    const sla: Record<string, string> = {};
    for (const pair of (hours.sla ?? "").replace(/[{}]/g, "").split(",")) {
      const [name = "", value = ""] = pair.split(":").map((item) => item.trim());
      sla[name] = value;
    }
    expect(Number(sla.standard_business_hours) * 60).toBe(STANDARD_BUSINESS_MINUTES);
    expect(Number(sla.urgent_minutes)).toBe(URGENT_MINUTES);
    expect(Number(sla.urgent_out_of_hours_minutes)).toBe(URGENT_OUT_OF_HOURS_MINUTES);
  });

  it("urgent reasons and intents", () => {
    expect(list(hours.urgent_reasons)).toEqual([...URGENT_REASONS]);
    expect(list(hours.urgent_intents)).toEqual([...URGENT_INTENTS]);
  });
});

describe("SLA", () => {
  it("counts only support hours (Bratislava, winter = UTC+1)", () => {
    // Monday 2026-11-23 18:00 → 21:00 local: 2 open hours (18–20).
    expect(businessMinutes(new Date("2026-11-23T17:00:00Z"), new Date("2026-11-23T20:00:00Z"))).toBe(120);
    // Sunday is closed.
    expect(businessMinutes(new Date("2026-11-22T09:00:00Z"), new Date("2026-11-22T15:00:00Z"))).toBe(0);
    // Saturday 10–16 only.
    expect(businessMinutes(new Date("2026-11-21T07:00:00Z"), new Date("2026-11-21T19:00:00Z"))).toBe(360);
    // Summer (UTC+2): Monday 2026-07-06 08:00 local is 06:00 UTC.
    expect(isOpen(new Date("2026-07-06T05:55:00Z"))).toBe(false);
    expect(isOpen(new Date("2026-07-06T06:00:00Z"))).toBe(true);
  });

  it("marks standard tickets after 4 business hours", () => {
    const now = new Date("2026-11-23T12:00:00Z");
    expect(isOverdue("2026-11-23T07:55:00Z", false, now)).toBe(true);
    // Saturday after closing to Monday 08:30 local: 30 business minutes.
    expect(isOverdue("2026-11-21T16:30:00Z", false, new Date("2026-11-23T07:30:00Z"))).toBe(false);
  });

  it("marks urgent tickets after 30 minutes in support hours", () => {
    const now = new Date("2026-11-23T12:00:00Z");
    expect(isOverdue("2026-11-23T11:25:00Z", true, now)).toBe(true);
    expect(isOverdue("2026-11-23T11:35:00Z", true, now)).toBe(false);
  });

  it("gives urgent tickets 60 minutes outside support hours, counted in clock time", () => {
    // Sunday 03:00 local (02:00 UTC): closed, so the on-call hour applies.
    expect(isOverdue("2026-11-22T02:00:00Z", true, new Date("2026-11-22T02:45:00Z"))).toBe(false);
    expect(isOverdue("2026-11-22T02:00:00Z", true, new Date("2026-11-22T03:05:00Z"))).toBe(true);
  });

  it("calls a ticket urgent as the policy does", () => {
    const base = { urgency: "normal", intent: "fees_fx", route_reasons: ["wants_human"] };
    expect(isUrgent(base)).toBe(false);
    expect(isUrgent({ ...base, urgency: "high" })).toBe(true);
    expect(isUrgent({ ...base, intent: "card_lost_stolen" })).toBe(true);
    expect(isUrgent({ ...base, intent: "account_restricted" })).toBe(true);
    expect(isUrgent({ ...base, route_reasons: ["fraud_report"] })).toBe(true);
    expect(isUrgent({ ...base, route_reasons: ["restricted_account"] })).toBe(true);
    expect(isUrgent({ urgency: null, intent: null, route_reasons: [] })).toBe(false);
  });

  it("writes the waiting time for people", () => {
    const now = new Date("2026-11-23T12:00:00Z");
    expect(waited("2026-11-23T11:48:00Z", now)).toBe("12 min");
    expect(waited("2026-11-23T09:30:00Z", now)).toBe("2 h 30 min");
    expect(waited("2026-11-20T12:00:00Z", now)).toBe("3 days");
  });
});
