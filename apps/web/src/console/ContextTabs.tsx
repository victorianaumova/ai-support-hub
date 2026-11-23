import { useState } from "react";
import { Link } from "react-router";
import { helpHref } from "../chat/MessageItem";
import { SafeText } from "../text/SafeText";
import { words } from "./labels";
import { draftParts, type Ticket } from "./useTicket";

const TABS = ["Sources", "Customer", "History", "Run"] as const;
type Tab = (typeof TABS)[number];

/** UI §3 right column. `focused` is the citation chip the operator clicked. */
export function ContextTabs({ ticket, focused }: { ticket: Ticket; focused: number | null }) {
  const [tab, setTab] = useState<Tab>("Sources");
  const current: Tab = focused !== null ? "Sources" : tab;
  const parts = draftParts(ticket.draft);
  const cited = new Map(parts.citations.map((c) => [c.source_id ?? "", c.marker]));
  const chunks = [...parts.chunks].sort((a, b) => Number(cited.has(b.chunk_id)) - Number(cited.has(a.chunk_id)) || a.rank - b.rank);
  const run = ticket.latest_run;
  const toolCalls = ticket.tool_calls ?? [];
  const memory = ticket.memory ?? [];
  const coreCalls = toolCalls.filter((c) => c.tool.startsWith("core_"));

  return (
    <section aria-label="Context" className="rounded-lg border border-slate-200 bg-white">
      <div role="tablist" aria-label="Context" className="flex border-b border-slate-200">
        {TABS.map((name) => (
          <button
            key={name}
            role="tab"
            type="button"
            aria-selected={current === name}
            onClick={() => {
              setTab(name);
            }}
            className={`flex-1 px-2 py-2 text-sm ${current === name ? "border-b-2 border-brand-600 font-semibold" : "text-muted"}`}
          >
            {name}
          </button>
        ))}
      </div>
      <div role="tabpanel" className="max-h-[70dvh] space-y-3 overflow-y-auto p-3 text-sm">
        {current === "Sources" && (
          <>
            {chunks.length === 0 && parts.facts.length === 0 && <p className="text-muted">No sources for this ticket.</p>}
            {chunks.map((chunk) => {
              const marker = cited.get(chunk.chunk_id);
              const href = helpHref(chunk.url);
              return (
                <article
                  key={chunk.chunk_id}
                  id={marker !== undefined ? `source-${marker}` : undefined}
                  className={`rounded-md border p-2 ${marker !== undefined ? "border-brand-600 bg-brand-50" : "border-slate-200"} ${marker === focused ? "ring-2 ring-brand-600" : ""}`}
                >
                  <p className="font-medium">
                    {marker !== undefined && <span className="mr-1 rounded-full bg-brand-600 px-1.5 text-xs text-white">{marker}</span>}
                    {href ? <Link to={href} target="_blank" rel="noopener noreferrer" className="underline">{chunk.title}</Link> : chunk.title}
                  </p>
                  <p className="text-xs text-muted">
                    {chunk.section} · score {chunk.score.toFixed(3)} · rank {chunk.rank}
                  </p>
                  <div className="mt-1">
                    <SafeText text={chunk.text} />
                  </div>
                </article>
              );
            })}
            {parts.facts.length > 0 && <h3 className="font-semibold">Account facts</h3>}
            {parts.facts.map((fact) => {
              const marker = cited.get(fact.fact_id);
              return (
                <article key={fact.fact_id} id={marker !== undefined ? `source-${marker}` : undefined} className={`rounded-md border p-2 ${marker !== undefined && marker === focused ? "ring-2 ring-brand-600" : "border-slate-200"}`}>
                  <p className="text-xs text-muted">
                    {fact.fact_id} · from {fact.source_tool}
                  </p>
                  <SafeText text={fact.text} />
                </article>
              );
            })}
          </>
        )}
        {current === "Customer" && (
          <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
            <dt className="text-muted">Name</dt>
            <dd>{ticket.customer.display_name}</dd>
            <dt className="text-muted">Reference</dt>
            <dd>{ticket.customer.customer_ref}</dd>
            <dt className="text-muted">Tier</dt>
            <dd className="capitalize">{ticket.customer.tier}</dd>
            <dt className="text-muted">Country</dt>
            <dd>{ticket.customer.country}</dd>
            <dt className="text-muted">Restricted</dt>
            <dd>{ticket.route_reasons.includes("restricted_account") ? "Yes (route reason)" : "Not flagged by this run"}</dd>
            <dt className="col-span-2 mt-2 font-semibold">Account data the AI read</dt>
            <dd className="col-span-2">
              {coreCalls.length === 0 ? (
                <span className="text-muted">None for this ticket.</span>
              ) : (
                <ul className="space-y-1">
                  {coreCalls.map((call) => (
                    <li key={call.id}>
                      <span className="font-mono text-xs">{call.tool}</span> — {call.result_summary ?? call.status}
                    </li>
                  ))}
                </ul>
              )}
            </dd>
          </dl>
        )}
        {current === "History" && (
          <>
            {memory.length === 0 && <p className="text-muted">No earlier tickets in memory.</p>}
            <ul className="space-y-2">
              {memory.map((item) => (
                <li key={item.ticket_id} className="rounded-md border border-slate-200 p-2">
                  <Link to={`/console/tickets/${item.ticket_id}`} className="font-medium text-brand-700 underline">
                    {words(item.intent)} · {new Date(item.created_at).toLocaleDateString()}
                  </Link>
                  <SafeText text={item.summary} />
                </li>
              ))}
            </ul>
          </>
        )}
        {current === "Run" && (
          <>
            {run === null ? (
              <p className="text-muted">No run yet.</p>
            ) : (
              <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
                <dt className="text-muted">Status</dt>
                <dd>{run.status}{run.error ? ` (${run.error})` : ""}</dd>
                <dt className="text-muted">Route</dt>
                <dd>{words(run.route)}</dd>
                <dt className="text-muted">Duration</dt>
                <dd>{run.latency_ms === null ? "—" : `${(run.latency_ms / 1000).toFixed(1)} s`}</dd>
                <dt className="text-muted">Cost</dt>
                <dd>${Number(run.total_cost_usd).toFixed(4)} · {run.input_tokens} in / {run.output_tokens} out tokens</dd>
                <dt className="text-muted">Models</dt>
                <dd>{Object.entries(run.models).map(([role, model]) => `${role}: ${model}`).join(", ") || "—"}</dd>
                <dt className="text-muted">Prompts</dt>
                <dd>{Object.entries(run.prompt_versions).map(([node, v]) => `${node} ${v}`).join(", ") || "—"}</dd>
                <dt className="text-muted">Trace</dt>
                <dd className="text-muted">Not available (tracing removed, ADR-0019)</dd>
              </dl>
            )}
            <h3 className="mt-3 font-semibold">Tool calls</h3>
            {toolCalls.length === 0 ? (
              <p className="text-muted">None.</p>
            ) : (
              <ul className="space-y-1">
                {toolCalls.map((call) => (
                  <li key={call.id}>
                    <span className="font-mono text-xs">{call.tool}</span> · {call.node} · {call.status}
                    {call.external_ref ? ` · ${call.external_ref}` : ""} · {call.latency_ms} ms
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </div>
    </section>
  );
}
