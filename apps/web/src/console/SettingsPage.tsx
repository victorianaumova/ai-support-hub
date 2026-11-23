import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, unwrap } from "../api/client";
import { ApiError, describe } from "../api/problem";
import type { components } from "../api/schema";
import { Dialog } from "../components/Dialog";
import { ErrorState } from "../components/ErrorState";
import { Loading } from "../components/Loading";
import { LastEvaluation } from "./DashboardPage";
import { words } from "./labels";

type Change = Omit<components["schemas"]["SettingsIn"], "reason">;

function changeText(change: Change): string {
  if (change.auto_reply_enabled !== undefined && change.auto_reply_enabled !== null) return `Switch auto-reply ${change.auto_reply_enabled ? "on" : "off"}`;
  if (change.confidence_threshold != null) return `Set the confidence threshold to ${change.confidence_threshold.toFixed(2)}`;
  if (change.daily_budget_usd != null) return `Set the daily budget to $${change.daily_budget_usd.toFixed(2)}`;
  return "Change the intents that may be auto-replied";
}

/** UI §4 settings. Every change asks for a short reason, which goes to the audit log. */
export function SettingsPage() {
  const client = useQueryClient();
  const settings = useQuery({ queryKey: ["console", "settings"], queryFn: async () => unwrap(await api.GET("/api/v1/admin/settings")) });
  const evals = useQuery({ queryKey: ["console", "evals"], queryFn: async () => unwrap(await api.GET("/api/v1/console/evals/latest")).run });
  const [pending, setPending] = useState<Change | null>(null);
  const [reason, setReason] = useState("");
  const [threshold, setThreshold] = useState<number | null>(null);
  const [budget, setBudget] = useState("");
  const save = useMutation({
    mutationFn: async (body: components["schemas"]["SettingsIn"]) => unwrap(await api.PUT("/api/v1/admin/settings", { body })),
    onSuccess: (data) => {
      client.setQueryData(["console", "settings"], data);
      setPending(null);
      setReason("");
    },
  });

  if (settings.isPending) return <Loading label="Loading the settings" />;
  if (settings.isError) return <ErrorState error={settings.error} onRetry={() => void settings.refetch()} />;
  const s = settings.data;
  const shownThreshold = threshold ?? s.confidence_threshold ?? 0.95;
  const ask = (change: Change) => {
    save.reset();
    setReason("");
    setPending(change);
  };

  return (
    <div className="mx-auto max-w-3xl px-4 py-6">
      <h1 className="text-2xl font-semibold">Settings</h1>
      <p className="mt-1 text-sm text-muted">Changes count from the next ticket. Each change is written to the audit log with your reason.</p>

      <section className="mt-6 rounded-lg border border-slate-200 bg-white p-4">
        <h2 className="font-semibold">Auto-reply</h2>
        <p className="mt-1 text-sm">
          Now: <strong>{s.auto_reply_enabled ? "on" : "off"}</strong>. When off, every draft waits for an operator's approval.
        </p>
        <button
          type="button"
          role="switch"
          aria-checked={s.auto_reply_enabled}
          onClick={() => { ask({ auto_reply_enabled: !s.auto_reply_enabled }); }}
          className={`mt-3 rounded-md px-4 py-2 font-medium ${s.auto_reply_enabled ? "bg-red-700 text-white" : "bg-brand-600 text-white"}`}
        >
          {s.auto_reply_enabled ? "Switch auto-reply off" : "Switch auto-reply on…"}
        </button>
      </section>

      <section className="mt-4 rounded-lg border border-slate-200 bg-white p-4">
        <h2 className="font-semibold">Confidence threshold</h2>
        <label htmlFor="threshold" className="mt-2 block text-sm">
          Auto-reply only at a confidence of at least <strong className="tabular-nums">{shownThreshold.toFixed(2)}</strong>
        </label>
        <input id="threshold" type="range" min={0.5} max={1} step={0.01} value={shownThreshold} onChange={(e) => { setThreshold(Number(e.target.value)); }} className="mt-1 w-full" />
        <p className="mt-1 text-xs text-muted">
          The last evaluation has no per-ticket confidences in its committed report, so how many tickets this threshold would auto-reply is not shown.
        </p>
        <button type="button" disabled={threshold === null || threshold === s.confidence_threshold} onClick={() => { ask({ confidence_threshold: shownThreshold }); }} className="mt-2 rounded-md border border-slate-300 px-3 py-1.5 text-sm disabled:opacity-50">
          Save threshold…
        </button>
      </section>

      <section className="mt-4 rounded-lg border border-slate-200 bg-white p-4">
        <h2 className="font-semibold">Daily model budget</h2>
        <form
          className="mt-2 flex flex-wrap items-end gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            const value = Number(budget);
            if (budget !== "" && Number.isFinite(value)) ask({ daily_budget_usd: value });
          }}
        >
          <label className="text-sm">
            USD per day (now {s.daily_budget_usd === null ? "not set" : `$${s.daily_budget_usd.toFixed(2)}`})
            <input type="number" min={0} max={100} step={0.01} value={budget} onChange={(e) => { setBudget(e.target.value); }} className="ml-2 w-28 rounded border border-slate-300 px-2 py-1" />
          </label>
          <button type="submit" disabled={budget === ""} className="rounded-md border border-slate-300 px-3 py-1.5 text-sm disabled:opacity-50">
            Save budget…
          </button>
        </form>
      </section>

      <section className="mt-4 rounded-lg border border-slate-200 bg-white p-4">
        <h2 className="font-semibold">Intents that may be auto-replied</h2>
        <p className="mt-1 text-xs text-muted">Only intents the policy allows can be listed; the others always need a human.</p>
        <fieldset className="mt-2 grid gap-1 sm:grid-cols-2">
          <legend className="sr-only">Allowed intents</legend>
          {s.allowed_intents.map((intent) => {
            const on = s.intent_allowlist.includes(intent);
            return (
              <label key={intent} className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={on}
                  onChange={() => {
                    ask({ intent_allowlist: on ? s.intent_allowlist.filter((i) => i !== intent) : [...s.intent_allowlist, intent] } as Change);
                  }}
                />
                {words(intent)}
              </label>
            );
          })}
        </fieldset>
      </section>

      {pending !== null && (
        <Dialog title={changeText(pending)} onClose={() => { setPending(null); }}>
          {pending.auto_reply_enabled === true && (
            <div className="mb-3 rounded-md bg-amber-50 p-3 text-sm">
              <p className="font-semibold">Check the last evaluation before customers get answers without review:</p>
              {evals.data === undefined ? <Loading /> : <LastEvaluation run={evals.data} />}
            </div>
          )}
          <form
            onSubmit={(event) => {
              event.preventDefault();
              save.mutate({ ...pending, reason: reason.trim() });
            }}
          >
            <label htmlFor="reason" className="block text-sm font-medium">
              Reason (required, goes to the audit log)
            </label>
            <textarea id="reason" required minLength={3} maxLength={500} rows={2} value={reason} onChange={(e) => { setReason(e.target.value); }} className="mt-1 w-full rounded-md border border-slate-300 p-2" />
            {save.isError && (
              <p role="alert" className="mt-2 text-sm text-red-900">
                {save.error instanceof ApiError && save.error.code === "validation_error" ? "The change was refused: check the value." : describe(save.error)}
              </p>
            )}
            <div className="mt-3 flex justify-end gap-2">
              <button type="button" onClick={() => { setPending(null); }} className="rounded-md border border-slate-300 px-3 py-1.5">
                Cancel
              </button>
              <button type="submit" disabled={reason.trim().length < 3 || save.isPending} className="rounded-md bg-brand-600 px-4 py-1.5 font-medium text-white disabled:opacity-50">
                Confirm change
              </button>
            </div>
          </form>
        </Dialog>
      )}
    </div>
  );
}
