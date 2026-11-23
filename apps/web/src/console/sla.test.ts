import { describe, expect, it } from "vitest";
import { businessMinutes, isOverdue, waited } from "./sla";

describe("SLA", () => {
  it("counts only support hours (Bratislava, winter = UTC+1)", () => {
    // Monday 2026-11-23 18:00 → 21:00 local: 2 open hours (18–20).
    expect(businessMinutes(new Date("2026-11-23T17:00:00Z"), new Date("2026-11-23T20:00:00Z"))).toBe(120);
    // Sunday is closed.
    expect(businessMinutes(new Date("2026-11-22T09:00:00Z"), new Date("2026-11-22T15:00:00Z"))).toBe(0);
    // Saturday 10–16 only.
    expect(businessMinutes(new Date("2026-11-21T07:00:00Z"), new Date("2026-11-21T19:00:00Z"))).toBe(360);
  });

  it("marks urgent tickets after 30 minutes and standard ones after 4 business hours", () => {
    const now = new Date("2026-11-23T12:00:00Z");
    expect(isOverdue("2026-11-23T11:25:00Z", true, now)).toBe(true);
    expect(isOverdue("2026-11-23T11:35:00Z", true, now)).toBe(false);
    expect(isOverdue("2026-11-23T07:55:00Z", false, now)).toBe(true);
    // Saturday after closing to Monday 08:30 local: 30 business minutes.
    expect(isOverdue("2026-11-21T16:30:00Z", false, new Date("2026-11-23T07:30:00Z"))).toBe(false);
  });

  it("writes the waiting time for people", () => {
    const now = new Date("2026-11-23T12:00:00Z");
    expect(waited("2026-11-23T11:48:00Z", now)).toBe("12 min");
    expect(waited("2026-11-23T09:30:00Z", now)).toBe("2 h 30 min");
    expect(waited("2026-11-20T12:00:00Z", now)).toBe("3 days");
  });
});
