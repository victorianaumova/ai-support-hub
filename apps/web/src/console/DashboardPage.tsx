import { useQuery } from "@tanstack/react-query";
import { type ReactNode, useState } from "react";
import { api, unwrap } from "../api/client";
import type { components } from "../api/schema";
import { ErrorState } from "../components/ErrorState";
import { Icon } from "../components/Icon";
import type { IconName } from "../components/icons.generated";
import { Loading } from "../components/Loading";
import { BarList, StackedDays } from "./charts";
import { reasonText } from "./labels";

type Metric = "routes" | "latency";

function isoDay(date: Date): string {
  return date.toISOString().slice(0, 10);
}

export function percent(value: number | null): string {
  return value === null ? "—" : `${(value * 100).toFixed(1)}%`;
}

export function seconds(ms: number | null): string {
  return ms === null ? "—" : `${(ms / 1000).toFixed(1)} s`;
}

export function usd(value: number | null, digits = 4): string {
  return value === null ? "—" : `$${value.toFixed(digits)}`;
}

const STEPS = ["stat-1", "stat-2", "stat-3", "stat-4", "stat-5"] as const;

/** A key-number tile; `step` picks its place on the pink → periwinkle band. */
function Card({ label, value, note, icon, step }: { label: string; value: ReactNode; note?: string; icon: IconName; step: number }) {
  return (
    <div className={`stat ${STEPS[step % STEPS.length] ?? "stat-1"}`}>
      <div className="flex items-center gap-3">
        <span className="stat-icon" aria-hidden="true">
          <Icon name={icon} size={20} />
        </span>
        <p className="leading-tight text-[var(--tb-text-2)]">{label}</p>
      </div>
      <p className="mt-4 text-[1.625rem] leading-tight font-medium tracking-[-0.01em] tabular-nums">{value}</p>
      {note !== undefined && <p className="mt-1 text-xs text-[var(--tb-text-2)]">{note}</p>}
    </div>
  );
}

function useSeries(metric: Metric, from: string, to: string) {
  return useQuery({
    queryKey: ["console", "series", metric, from, to],
    queryFn: async () => unwrap(await api.GET("/api/v1/console/metrics/timeseries", { params: { query: { metric, bucket: "day", from, to } } })),
  });
}

export function LastEvaluation({ run }: { run: components["schemas"]["EvalSummary"] | null }) {
  if (run === null) return <p className="text-sm text-muted">No evaluation report is available.</p>;
  return (
    <>
      <p className="text-sm text-muted">
        {run.date} · {run.variant} · commit {run.git_sha} · {run.tickets_finished} tickets · demo check, not a proof
      </p>
      <table className="data-table mt-3 text-sm">
        <thead>
          <tr>
            <th scope="col">Metric</th>
            <th scope="col">Result</th>
            <th scope="col">Target</th>
            <th scope="col">Red line</th>
            <th scope="col">Status</th>
          </tr>
        </thead>
        <tbody>
          {run.checks.map((check) => (
            <tr key={check.name}>
              <th scope="row" className="text-left font-normal text-ink">
                {check.name}
                {check.judged && <span className="ml-1 text-xs text-muted">(judged)</span>}
              </th>
              <td>{check.shown}</td>
              <td>{check.target_text}</td>
              <td>{check.red_line_text}</td>
              <td>
                <span className={`chip ${check.status === "red line missed" ? "chip-danger" : check.status === "pass" ? "chip-ok" : ""}`}>
                  {check.status === "pass" ? "✓ " : check.status === "red line missed" ? "✗ " : ""}
                  {check.status}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

/** UI §4 dashboard: every number comes from `/console/metrics/*` as the API sends it. */
export function DashboardPage() {
  const today = new Date();
  const [from, setFrom] = useState(isoDay(new Date(today.getTime() - 6 * 86_400_000)));
  const [to, setTo] = useState(isoDay(today));
  const summary = useQuery({
    queryKey: ["console", "summary", from, to],
    queryFn: async () => unwrap(await api.GET("/api/v1/console/metrics/summary", { params: { query: { from, to } } })),
  });
  const routes = useSeries("routes", from, to);
  const latency = useSeries("latency", from, to);
  const evals = useQuery({ queryKey: ["console", "evals"], queryFn: async () => unwrap(await api.GET("/api/v1/console/evals/latest")).run });

  return (
    <div className="px-4 py-5 sm:px-8 md:py-8 lg:px-10">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-3">
        <h1 className="flex min-h-12 items-center page-title">Dashboard</h1>
        <fieldset className="flex flex-wrap items-center gap-3 text-muted sm:ml-auto">
          <legend className="sr-only">Period</legend>
          <label>
            From
            <input type="date" value={from} max={to} onChange={(e) => { if (e.target.value) setFrom(e.target.value); }} className="ml-2 field rounded-full" />
          </label>
          <label>
            To
            <input type="date" value={to} min={from} onChange={(e) => { if (e.target.value) setTo(e.target.value); }} className="ml-2 field rounded-full" />
          </label>
        </fieldset>
      </div>
      {summary.isPending && <Loading label="Loading the numbers" />}
      {summary.isError && <ErrorState error={summary.error} onRetry={() => void summary.refetch()} />}
      {summary.data && (
        <>
          <section aria-label="Key numbers" className="mt-6 grid grid-cols-[repeat(auto-fit,minmax(12rem,1fr))] gap-2">
            <Card icon="inbox" step={0} label="Tickets" value={String(summary.data.tickets.total)} note={`${summary.data.tickets.auto_reply} auto · ${summary.data.tickets.approval} approval · ${summary.data.tickets.handoff} handoff`} />
            <Card icon="smart_toy" step={1} label="Automation share" value={percent(summary.data.automation_share)} note={`AI-resolved ${percent(summary.data.ai_resolved_share)}`} />
            <Card icon="timer" step={2} label="Median time to answer" value={seconds(summary.data.first_response_ms.p50)} note={`p95 ${seconds(summary.data.first_response_ms.p95)}`} />
            <Card icon="payments" step={3} label="Cost per ticket" value={usd(summary.data.cost_usd.per_ticket)} note={`total ${usd(summary.data.cost_usd.total)}`} />
            <Card
              icon="account_balance_wallet"
              step={4}
              label="Budget used today"
              value={
                <>
                  {usd(summary.data.budget.spent_today_usd)}
                  <span className="text-sm font-normal text-[var(--tb-text-2)]"> of {usd(summary.data.budget.daily_limit_usd, 2)}</span>
                </>
              }
              note={summary.data.budget.daily_limit_usd > 0 ? percent(summary.data.budget.spent_today_usd / summary.data.budget.daily_limit_usd) : "budget 0: every model call is refused"}
            />
          </section>
          <div className="mt-6 grid gap-4 lg:grid-cols-2">
            {routes.data && (
              <StackedDays
                title="Routes per day"
                series={[
                  { key: "auto_reply", label: "Auto-reply", className: "bg-accent" },
                  { key: "approval", label: "Approval", className: "bg-accent-3" },
                  { key: "handoff", label: "Handoff", className: "bg-accent-2" },
                ]}
                points={routes.data.points.map((p) => ({ date: p.date, values: p.values }))}
              />
            )}
            {latency.data && (
              <StackedDays
                title="Run latency per day (ms; p50 and the extra to p95)"
                series={[
                  { key: "p50", label: "p50", className: "bg-accent" },
                  { key: "extra", label: "to p95", className: "bg-accent-3" },
                ]}
                points={latency.data.points.map((p) => {
                  const p50 = (p.values.p50 as number | null) ?? null;
                  const p95 = (p.values.p95 as number | null) ?? null;
                  return { date: p.date, values: { p50, extra: p50 === null || p95 === null ? null : p95 - p50 } };
                })}
              />
            )}
            <BarList title="Route reasons" bars={summary.data.route_reasons.map((r) => ({ label: reasonText(r.reason), value: r.count }))} />
            <BarList
              title="Draft outcomes"
              bars={[
                { label: "Approved unchanged", value: summary.data.drafts.approved },
                { label: "Edited", value: summary.data.drafts.edited, shown: `${summary.data.drafts.edited} (median edit ${summary.data.drafts.median_edit_ratio ?? "—"})` },
                { label: "Rejected", value: summary.data.drafts.rejected },
              ]}
            />
            <BarList title="Cost by node (USD)" bars={Object.entries(summary.data.cost_usd.by_node).map(([node, cost]) => ({ label: node, value: cost, shown: usd(cost) }))} />
            <BarList
              title="Customer feedback"
              bars={[
                { label: "Helpful", value: summary.data.feedback.up },
                { label: "Not helpful", value: summary.data.feedback.down },
              ]}
            />
          </div>
          <p className="mt-3 text-xs text-muted">
            Run latency median {seconds(summary.data.latency_ms.p50)}, p95 {seconds(summary.data.latency_ms.p95)}. Cost counts real model calls only.
          </p>
        </>
      )}
      <section aria-labelledby="eval-heading" className="mt-6 panel p-6">
        <h2 id="eval-heading" className="text-lg font-medium">
          Last evaluation
        </h2>
        {evals.isError ? <ErrorState error={evals.error} /> : evals.data === undefined ? <Loading /> : <LastEvaluation run={evals.data} />}
      </section>
    </div>
  );
}
