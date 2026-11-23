import { useEffect, useRef, useState } from "react";
import { Link } from "react-router";
import { ErrorState } from "../components/ErrorState";
import { Loading } from "../components/Loading";
import { INTENTS, REASONS, reasonText, words } from "./labels";
import { type Filters, type QueueRow, rememberOrder, TABS, type TabId, useQueue } from "./queue";
import { isOverdue, isUrgent, waited } from "./sla";

const EMPTY: Filters = { intent: "", urgency: "", reason: "", q: "" };

function Counter({ tab, filters }: { tab: TabId; filters: Filters }) {
  const { data } = useQueue(tab, filters);
  if (!data) return null;
  return <span className="ml-1 rounded-full bg-slate-200 px-2 text-xs">{data.items.length}{data.next_cursor ? "+" : ""}</span>;
}

function Row({ row, isNew, now }: { row: QueueRow; isNew: boolean; now: Date }) {
  const urgent = isUrgent(row);
  const late = isOverdue(row.waiting_since, urgent, now);
  return (
    <tr className={`border-t border-slate-200 ${isNew ? "bg-amber-50" : "bg-white"}`}>
      <td className="p-2 align-top">
        {urgent ? (
          <span className="rounded bg-red-100 px-1.5 py-0.5 text-xs font-semibold text-red-900">⚑ Urgent</span>
        ) : (
          <span className="text-xs text-muted">{words(row.urgency)}</span>
        )}
        {isNew && <span className="ml-1 rounded bg-amber-200 px-1 text-xs font-semibold text-amber-950">New</span>}
      </td>
      <td className="p-2 align-top">
        <Link to={`/console/tickets/${row.id}`} className="font-medium text-brand-700 underline-offset-2 hover:underline">
          {row.customer.display_name}
        </Link>
        <span className="block text-xs capitalize text-muted">
          {row.customer.tier} · {row.customer.customer_ref}
        </span>
      </td>
      <td className="p-2 align-top text-sm">
        <span className="font-medium">{words(row.intent)}</span>
        <span className="block max-w-md truncate text-muted">{row.summary?.split("\n")[0] ?? "No summary yet"}</span>
      </td>
      <td className="p-2 align-top">
        <ul className="flex flex-wrap gap-1">
          {row.route_reasons.map((code) => (
            <li key={code} title={reasonText(code)} className="rounded bg-slate-100 px-1.5 py-0.5 text-xs">
              {words(code)}
            </li>
          ))}
        </ul>
      </td>
      <td className={`whitespace-nowrap p-2 align-top text-sm ${late ? "font-semibold text-red-800" : ""}`}>
        {waited(row.waiting_since, now)}
        {late && <span className="block text-xs">over SLA</span>}
      </td>
      <td className="p-2 align-top text-sm">{row.assigned_operator?.name ?? <span className="text-muted">—</span>}</td>
    </tr>
  );
}

export function QueuePage() {
  const [tab, setTab] = useState<TabId>("approval");
  const [filters, setFilters] = useState<Filters>(EMPTY);
  const queue = useQueue(tab, filters);
  const seen = useRef<Set<string> | null>(null);
  const [newIds, setNewIds] = useState<Set<string>>(new Set());
  const now = new Date(queue.dataUpdatedAt || 0);

  useEffect(() => {
    if (!queue.data) return;
    const ids = queue.data.items.map((r) => r.id);
    rememberOrder(ids);
    if (seen.current === null) {
      seen.current = new Set(ids);
      return;
    }
    const fresh = ids.filter((id) => !seen.current?.has(id));
    for (const id of fresh) seen.current.add(id);
    if (fresh.length > 0) setNewIds((current) => new Set([...current, ...fresh]));
  }, [queue.data]);

  const set = (key: keyof Filters) => (value: string) => {
    setFilters((current) => ({ ...current, [key]: value }));
  };

  return (
    <div className="mx-auto max-w-7xl px-4 py-6">
      <h1 className="text-2xl font-semibold">Ticket queue</h1>
      <div role="tablist" aria-label="Queues" className="mt-4 flex flex-wrap gap-1 border-b border-slate-200">
        {TABS.map((t) => (
          <button
            key={t.id}
            role="tab"
            type="button"
            aria-selected={tab === t.id}
            onClick={() => {
              setTab(t.id);
              seen.current = null;
              setNewIds(new Set());
            }}
            className={`-mb-px rounded-t-md border px-3 py-2 text-sm ${tab === t.id ? "border-slate-200 border-b-white bg-white font-semibold" : "border-transparent text-muted hover:bg-slate-100"}`}
          >
            {t.label}
            <Counter tab={t.id} filters={filters} />
          </button>
        ))}
      </div>
      <form role="search" className="mt-4 flex flex-wrap gap-3" onSubmit={(e) => { e.preventDefault(); }}>
        <label className="text-sm">
          Search
          <input value={filters.q} maxLength={200} onChange={(e) => { set("q")(e.target.value); }} placeholder="Summary, name or CUS-…" className="ml-2 rounded-md border border-slate-300 px-2 py-1" />
        </label>
        <label className="text-sm">
          Intent
          <select value={filters.intent} onChange={(e) => { set("intent")(e.target.value); }} className="ml-2 rounded-md border border-slate-300 px-2 py-1">
            <option value="">Any</option>
            {INTENTS.map((i) => <option key={i} value={i}>{words(i)}</option>)}
          </select>
        </label>
        <label className="text-sm">
          Urgency
          <select value={filters.urgency} onChange={(e) => { set("urgency")(e.target.value); }} className="ml-2 rounded-md border border-slate-300 px-2 py-1">
            <option value="">Any</option>
            <option value="high">high</option>
            <option value="normal">normal</option>
            <option value="low">low</option>
          </select>
        </label>
        <label className="text-sm">
          Reason
          <select value={filters.reason} onChange={(e) => { set("reason")(e.target.value); }} className="ml-2 rounded-md border border-slate-300 px-2 py-1">
            <option value="">Any</option>
            {Object.keys(REASONS).map((r) => <option key={r} value={r}>{words(r)}</option>)}
          </select>
        </label>
      </form>
      <div role="tabpanel" className="mt-4 overflow-x-auto rounded-lg border border-slate-200">
        {queue.isPending ? (
          <Loading label="Loading the queue" />
        ) : queue.isError ? (
          <ErrorState error={queue.error} onRetry={() => void queue.refetch()} />
        ) : queue.data.items.length === 0 ? (
          <p className="p-6 text-muted">No tickets here.</p>
        ) : (
          <table className="w-full min-w-[48rem] text-left">
            <caption className="sr-only">Tickets, most urgent and longest waiting first. Refreshes every 5 seconds.</caption>
            <thead className="bg-slate-50 text-xs uppercase tracking-wide text-muted">
              <tr>
                <th scope="col" className="p-2">Urgency</th>
                <th scope="col" className="p-2">Customer</th>
                <th scope="col" className="p-2">Intent and summary</th>
                <th scope="col" className="p-2">Reasons</th>
                <th scope="col" className="p-2">Waiting</th>
                <th scope="col" className="p-2">Assignee</th>
              </tr>
            </thead>
            <tbody>
              {queue.data.items.map((row) => <Row key={row.id} row={row} isNew={newIds.has(row.id)} now={now} />)}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
