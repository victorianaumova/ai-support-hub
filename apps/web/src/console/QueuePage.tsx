import { useEffect, useRef, useState } from "react";
import { Link } from "react-router";
import { ErrorState } from "../components/ErrorState";
import { Icon } from "../components/Icon";
import { Loading } from "../components/Loading";
import { INTENTS, REASONS, reasonText, words } from "./labels";
import { type Filters, type QueueRow, rememberOrder, TABS, type TabId, useQueue } from "./queue";
import { isOverdue, isUrgent, waited } from "./sla";

const EMPTY: Filters = { intent: "", urgency: "", reason: "", q: "" };

function Counter({ tab, filters }: { tab: TabId; filters: Filters }) {
  const { data } = useQueue(tab, filters);
  if (!data) return null;
  return <span className="ml-2 inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-[var(--tb-line)] px-1.5 text-xs font-medium tabular-nums text-[var(--tb-text-2)] [[aria-selected=true]_&]:bg-primary [[aria-selected=true]_&]:text-white">{data.items.length}{data.next_cursor ? "+" : ""}</span>;
}

function Row({ row, isNew, now }: { row: QueueRow; isNew: boolean; now: Date }) {
  const urgent = isUrgent(row);
  const late = isOverdue(row.waiting_since, urgent, now);
  return (
    <tr className={`h-[var(--tb-row-height)] ${isNew ? "bg-amber-50" : ""}`}>
      <td>
        {urgent ? (
          <span className="inline-flex items-center gap-1.5 font-medium text-red-800">
            <Icon name="flag" size={18} />
            Urgent
          </span>
        ) : (
          row.urgency ? <span className="capitalize">{words(row.urgency)}</span> : <span className="text-muted">—</span>
        )}
        {isNew && <span className="chip chip-warn ml-2">New</span>}
      </td>
      <td>
        <Link to={`/console/tickets/${row.id}`} className="whitespace-nowrap font-medium text-ink underline-offset-4 hover:text-primary-text hover:underline">
          {row.customer.display_name}
        </Link>
        <span className="block whitespace-nowrap text-xs capitalize text-muted">
          {row.customer.tier} · {row.customer.customer_ref}
        </span>
      </td>
      <td>
        <span className="capitalize">{words(row.intent)}</span>
        <span className="block max-w-md truncate text-muted">{row.summary?.split("\n")[0] ?? "No summary yet"}</span>
      </td>
      <td>
        <ul className="flex flex-wrap gap-1">
          {row.route_reasons.map((code) => (
            <li key={code} title={reasonText(code)} className="chip chip-info">
              {words(code)}
            </li>
          ))}
        </ul>
      </td>
      <td className={`whitespace-nowrap ${late ? "font-medium text-red-800" : ""}`}>
        <span className="inline-flex items-center gap-1.5">
          <Icon name={late ? "error" : "schedule"} size={18} className={late ? "" : "text-muted"} />
          {waited(row.waiting_since, now)}
        </span>
        {late && <span className="block pl-6 text-xs">over SLA</span>}
      </td>
      <td>{row.assigned_operator?.name ?? <span className="text-muted">—</span>}</td>
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
    <div className="px-4 py-5 sm:px-8 md:py-8 lg:px-10">
      <div className="flex min-h-12 flex-wrap items-center gap-x-4 gap-y-1">
        <h1 className="page-title">Ticket queue</h1>
        <p className="text-muted">Most urgent and longest waiting first · refreshes every 5 s</p>
      </div>
      <div role="tablist" aria-label="Queues" className="seg mt-6 max-w-full flex-wrap">
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
            className="seg-tab"
          >
            {t.label}
            <Counter tab={t.id} filters={filters} />
          </button>
        ))}
      </div>
      <form role="search" className="mt-4 flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-line pb-4" onSubmit={(e) => { e.preventDefault(); }}>
        <label className="flex h-10 min-w-[16rem] flex-1 items-center gap-2 rounded-full bg-white px-4 text-muted shadow-lift focus-within:outline-2 focus-within:outline-[var(--tb-primary-text)]">
          <Icon name="search" />
          <span className="sr-only">Filter</span>
          <input value={filters.q} maxLength={200} onChange={(e) => { set("q")(e.target.value); }} placeholder="Search summary, name or CUS-…" aria-label="Search" className="h-full min-w-0 flex-1 border-0 bg-transparent text-ink outline-none" />
        </label>
        <label className="flex items-center text-muted">
          Intent
          <select value={filters.intent} onChange={(e) => { set("intent")(e.target.value); }} className="field ml-2 rounded-full bg-white px-3 text-ink">
            <option value="">Any</option>
            {INTENTS.map((i) => <option key={i} value={i}>{words(i)}</option>)}
          </select>
        </label>
        <label className="flex items-center text-muted">
          Urgency
          <select value={filters.urgency} onChange={(e) => { set("urgency")(e.target.value); }} className="field ml-2 rounded-full bg-white px-3 text-ink">
            <option value="">Any</option>
            <option value="high">high</option>
            <option value="normal">normal</option>
            <option value="low">low</option>
          </select>
        </label>
        <label className="flex items-center text-muted">
          Reason
          <select value={filters.reason} onChange={(e) => { set("reason")(e.target.value); }} className="field ml-2 rounded-full bg-white px-3 text-ink">
            <option value="">Any</option>
            {Object.keys(REASONS).map((r) => <option key={r} value={r}>{words(r)}</option>)}
          </select>
        </label>
      </form>
      <div role="tabpanel" className="overflow-x-auto">
        {queue.isPending ? (
          <Loading label="Loading the queue" />
        ) : queue.isError ? (
          <ErrorState error={queue.error} onRetry={() => void queue.refetch()} />
        ) : queue.data.items.length === 0 ? (
          <div className="flex flex-col items-center px-6 py-12 text-center">
            <span aria-hidden="true" className="flex size-14 items-center justify-center rounded-full bg-gradient-to-br from-[#f8dce7] to-[#dce0f8] text-ink">
              <Icon name="inbox" size={24} />
            </span>
            <p className="mt-4 text-lg font-medium">No tickets here.</p>
            <p className="mt-1 text-muted">New tickets appear on their own; the list refreshes every 5 seconds.</p>
          </div>
        ) : (
          <table className="data-table min-w-[48rem]">
            <caption className="sr-only">Tickets, most urgent and longest waiting first. Refreshes every 5 seconds.</caption>
            <thead>
              <tr>
                <th scope="col">Urgency</th>
                <th scope="col">Customer</th>
                <th scope="col">Intent and summary</th>
                <th scope="col">Reasons</th>
                <th scope="col">Waiting</th>
                <th scope="col">Assignee</th>
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
