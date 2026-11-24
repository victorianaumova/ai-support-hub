import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";
import { api, unwrap } from "../api/client";
import type { components } from "../api/schema";
import { helpHref } from "../chat/MessageItem";
import { SafeText } from "../text/SafeText";
import { words } from "./labels";
import { draftParts, type Ticket } from "./useTicket";

const TABS = ["Sources", "Customer", "History", "Run"] as const;
type Tab = (typeof TABS)[number];

type Account = components["schemas"]["AccountOut"];

const money = (amount: string | number, currency: string) => `${Number(amount).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ${currency}`;

/** Yes / No from the core profile; "Unknown" whenever the profile could not be read — never
 * "No" for data we do not have. A `restricted_account` route reason is a yes on its own. */
export function restrictedText(account: Account | undefined, routeReasons: string[]): string {
  if (routeReasons.includes("restricted_account")) return "Yes";
  if (account?.profile == null) return "Unknown";
  return account.profile.restricted ? "Yes" : "No";
}

function Unknown({ what }: { what: string }) {
  return <p className="text-muted">Unknown: the {what} could not be read from the core system.</p>;
}

/** UI §3 *Customer* tab: the profile of the ticket, and limits used, cards and the newest
 * transactions read now from the core tools (`GET /console/tickets/{id}/account`, once per page). */
function CustomerTab({ ticket }: { ticket: Ticket }) {
  const account = useQuery({
    queryKey: ["console", "account", ticket.id],
    queryFn: async () => unwrap(await api.GET("/api/v1/console/tickets/{ticket_id}/account", { params: { path: { ticket_id: ticket.id } } })),
    // Each read is three recorded tool calls with a per-run limit: read once per page.
    staleTime: Infinity,
    retry: false,
    refetchOnWindowFocus: false,
  });
  const data = account.data;
  const loading = account.isPending;
  return (
    <div className="space-y-3">
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
        <dd>{loading ? "Loading…" : restrictedText(data, ticket.route_reasons)}</dd>
      </dl>
      {loading && <p className="text-muted">Reading the account…</p>}
      {account.isError && <p role="alert" className="text-red-900">The account data could not be loaded, so limits, cards and transactions are unknown.</p>}
      {data !== undefined && (
        <>
          <h3 className="font-medium">Limits used</h3>
          {data.profile === null ? (
            <Unknown what="limits" />
          ) : (
            <table className="w-full text-left">
              <caption className="sr-only">Limits and how much of each is used, as of {data.profile.as_of}</caption>
              <thead className="text-xs text-muted">
                <tr><th scope="col">Limit</th><th scope="col">Used</th><th scope="col">Limit (EUR)</th></tr>
              </thead>
              <tbody>
                {data.profile.limits.map((limit) => (
                  <tr key={limit.name}>
                    <th scope="row" className="font-normal">{words(limit.name)}</th>
                    <td>{Object.keys(limit.used).length === 0 ? "—" : Object.entries(limit.used).map(([currency, amount]) => money(amount, currency)).join(", ")}</td>
                    <td className="tabular-nums">{money(limit.limit_eur, "EUR")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <h3 className="font-medium">Cards</h3>
          {data.cards === null ? (
            <Unknown what="cards" />
          ) : data.cards.length === 0 ? (
            <p className="text-muted">No cards.</p>
          ) : (
            <ul className="space-y-1">
              {data.cards.map((card) => (
                <li key={card.card_ref}>
                  <span className="capitalize">{card.kind}</span> •••• {card.last4} · {words(card.status)}
                  {card.delivery_status !== null && card.delivery_status !== "delivered" && (
                    <> · delivery {words(card.delivery_status)}{card.expected_delivery_by !== null ? ` by ${card.expected_delivery_by}` : ""}{card.delivery_overdue ? " (overdue)" : ""}</>
                  )}
                </li>
              ))}
            </ul>
          )}
          <h3 className="font-medium">Recent transactions</h3>
          {data.transactions === null ? (
            <Unknown what="transactions" />
          ) : data.transactions.length === 0 ? (
            <p className="text-muted">No transactions.</p>
          ) : (
            <ul className="space-y-1">
              {data.transactions.map((tx) => (
                <li key={tx.reference}>
                  <span className="font-mono text-xs">{tx.reference}</span> · {words(tx.type)} · {money(tx.amount, tx.currency)} · {words(tx.status)}
                  {tx.error_code !== null ? ` · ${tx.error_code}` : ""} · {new Date(tx.requested_at).toLocaleDateString()}
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  );
}

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
  const modelCalls = ticket.model_calls ?? [];

  return (
    <section aria-label="Context" className="panel self-start">
      <div role="tablist" aria-label="Context" className="flex border-b border-line">
        {TABS.map((name) => (
          <button
            key={name}
            role="tab"
            type="button"
            aria-selected={current === name}
            onClick={() => {
              setTab(name);
            }}
            className={`-mb-px h-12 flex-1 border-b-2 px-2 font-medium ${current === name ? "border-primary text-primary-text" : "border-transparent text-muted hover:bg-surface-hover hover:text-ink"}`}
          >
            {name}
          </button>
        ))}
      </div>
      <div role="tabpanel" className="max-h-[70dvh] space-y-3 overflow-y-auto p-4">
        {current === "Sources" && (
          <>
            {chunks.length === 0 && parts.facts.length === 0 && <p className="text-muted">No sources for this ticket.</p>}
            {chunks.map((chunk) => {
              const marker = cited.get(chunk.chunk_id);
              // The help pages hide internal articles (404): their title stays plain text.
              const href = chunk.visibility === "internal" ? null : helpHref(chunk.url);
              return (
                <article
                  key={chunk.chunk_id}
                  id={marker !== undefined ? `source-${marker}` : undefined}
                  className={`rounded-md border p-2 ${marker !== undefined ? "border-sky-300 bg-sky-50" : "border-line"} ${marker === focused ? "ring-2 ring-primary" : ""}`}
                >
                  <p className="font-medium">
                    {marker !== undefined && <span className="mr-1 inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-primary px-1.5 text-xs text-white">{marker}</span>}
                    {href ? <Link to={href} target="_blank" rel="noopener noreferrer" className="text-primary-text hover:underline">{chunk.title}</Link> : chunk.title}
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
            {parts.facts.length > 0 && <h3 className="font-medium">Account facts</h3>}
            {parts.facts.map((fact) => {
              const marker = cited.get(fact.fact_id);
              return (
                <article key={fact.fact_id} id={marker !== undefined ? `source-${marker}` : undefined} className={`rounded-md border p-2 ${marker !== undefined && marker === focused ? "ring-2 ring-primary" : "border-line"}`}>
                  <p className="text-xs text-muted">
                    {fact.fact_id} · from {fact.source_tool}
                  </p>
                  <SafeText text={fact.text} />
                </article>
              );
            })}
          </>
        )}
        {current === "Customer" && <CustomerTab ticket={ticket} />}
        {current === "History" && (
          <>
            {memory.length === 0 && <p className="text-muted">No earlier tickets in memory.</p>}
            <ul className="space-y-2">
              {memory.map((item) => (
                <li key={item.ticket_id} className="rounded-md border border-line p-2">
                  <Link to={`/console/tickets/${item.ticket_id}`} className="font-medium text-primary-text hover:underline">
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
                <dd>
                  {ticket.trace_url ? (
                    <a href={ticket.trace_url} target="_blank" rel="noopener noreferrer" className="text-primary-text hover:underline">
                      Open in Langfuse
                    </a>
                  ) : run.trace_id ? (
                    <span className="font-mono text-xs">{run.trace_id}</span>
                  ) : (
                    <span className="text-muted">Not traced</span>
                  )}
                </dd>
              </dl>
            )}
            {run !== null && (
              <>
                <h3 className="mt-3 font-medium">Steps</h3>
                {run.steps.length === 0 ? (
                  <p className="text-muted">No steps recorded.</p>
                ) : (
                  <ol className="space-y-1">
                    {run.steps.map((step, index) => (
                      <li key={`${step.node}-${index}`}>
                        {step.node} · {step.duration_ms} ms{step.outcome === "ok" ? "" : ` · ${step.outcome}`}
                      </li>
                    ))}
                  </ol>
                )}
              </>
            )}
            <h3 className="mt-3 font-medium">Model calls</h3>
            {modelCalls.length === 0 ? (
              <p className="text-muted">None.</p>
            ) : (
              <ul className="space-y-1">
                {modelCalls.map((call) => (
                  <li key={call.id}>
                    {call.node} · <span className="font-mono text-xs">{call.model}</span>
                    {call.prompt_version !== null ? ` · ${call.prompt_version}` : ""} · {call.status} · {call.latency_ms} ms · ${Number(call.cost_usd).toFixed(4)}
                  </li>
                ))}
              </ul>
            )}
            <h3 className="mt-3 font-medium">Tool calls</h3>
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
