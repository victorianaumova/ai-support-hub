import { type InfiniteData, useInfiniteQuery, useMutation } from "@tanstack/react-query";
import { type SubmitEvent, useState } from "react";
import { api, unwrap } from "../api/client";
import type { components, paths } from "../api/schema";
import { Dialog } from "../components/Dialog";
import { ErrorState } from "../components/ErrorState";
import { Loading } from "../components/Loading";

type Row = components["schemas"]["AuditOut"];
type Page = components["schemas"]["Page_AuditOut_"];
type AuditQuery = NonNullable<paths["/api/v1/console/audit"]["get"]["parameters"]["query"]>;
interface Filters {
  entity_type: string;
  entity_id: string;
  actor_id: string;
  action: string;
  from: string;
  to: string;
}
const EMPTY: Filters = { entity_type: "", entity_id: "", actor_id: "", action: "", from: "", to: "" };

/** UI §5: audit log with filters, a detail drawer with the JSON, and the chain check. */
export function AuditPage() {
  const [draft, setDraft] = useState<Filters>(EMPTY);
  const [filters, setFilters] = useState<Filters>(EMPTY);
  const [open, setOpen] = useState<Row | null>(null);
  const log = useInfiniteQuery<Page, Error, InfiniteData<Page>, readonly unknown[], string | null>({
    queryKey: ["console", "audit", filters],
    initialPageParam: null,
    getNextPageParam: (page) => page.next_cursor ?? null,
    queryFn: async ({ pageParam }): Promise<Page> => {
      const query: AuditQuery = { limit: 50 };
      for (const key of ["entity_type", "entity_id", "actor_id", "action"] as const) {
        if (filters[key] !== "") query[key] = filters[key];
      }
      if (filters.from !== "") query.from = new Date(filters.from).toISOString();
      if (filters.to !== "") query.to = new Date(filters.to).toISOString();
      if (pageParam !== null) query.cursor = pageParam;
      return unwrap(await api.GET("/api/v1/console/audit", { params: { query } }));
    },
  });
  const verify = useMutation({ mutationFn: async () => unwrap(await api.GET("/api/v1/console/audit/verify")) });
  const rows = log.data?.pages.flatMap((p) => p.items) ?? [];

  const submit = (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    setFilters(draft);
  };
  const field = (key: keyof Filters, label: string, type = "text") => (
    <label className="text-sm">
      {label}
      <input type={type} value={draft[key]} maxLength={100} onChange={(e) => { setDraft((d) => ({ ...d, [key]: e.target.value })); }} className="mt-1 block w-44 field" />
    </label>
  );

  return (
    <div className="px-4 py-5 sm:px-8 md:py-8 lg:px-10">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="flex h-12 items-center page-title">Audit log</h1>
        <button type="button" onClick={() => { verify.mutate(); }} disabled={verify.isPending} className="ml-auto btn btn-secondary">
          {verify.isPending ? "Checking…" : "Verify chain"}
        </button>
      </div>
      {verify.data && (
        <p role="status" className={`mt-2 rounded-md p-3 text-sm ${verify.data.ok ? "bg-emerald-50 text-emerald-950" : "bg-red-50 text-red-900"}`}>
          {verify.data.ok ? `✓ OK: ${verify.data.checked} records verified.` : `✗ Broken: the first record that does not verify is #${verify.data.broken_id ?? "?"} (${verify.data.checked} verified before it).`}
        </p>
      )}
      {verify.isError && <ErrorState error={verify.error} />}
      <form onSubmit={submit} className="mt-4 flex flex-wrap items-end gap-3" aria-label="Filters">
        {field("action", "Action")}
        {field("entity_type", "Entity type")}
        {field("entity_id", "Entity id")}
        {field("actor_id", "Actor id")}
        {field("from", "From", "datetime-local")}
        {field("to", "To", "datetime-local")}
        <button type="submit" className="btn btn-primary">
          Filter
        </button>
      </form>
      <div className="mt-4 overflow-x-auto">
        {log.isPending ? (
          <Loading label="Loading the audit log" />
        ) : log.isError ? (
          <ErrorState error={log.error} onRetry={() => void log.refetch()} />
        ) : rows.length === 0 ? (
          <p className="p-6 text-muted">No records.</p>
        ) : (
          <table className="data-table min-w-[48rem]">
            <thead>
              <tr>
                <th scope="col">#</th>
                <th scope="col">Time</th>
                <th scope="col">Actor</th>
                <th scope="col">Action</th>
                <th scope="col">Entity</th>
                <th scope="col"><span className="sr-only">Details</span></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td className="tabular-nums">{row.id}</td>
                  <td>{new Date(row.ts).toLocaleString()}</td>
                  <td>{row.actor_type}{row.actor_id ? ` · ${row.actor_id.slice(0, 8)}` : ""}</td>
                  <td className="font-mono text-xs">{row.action}</td>
                  <td>{row.entity_type} · {row.entity_id.slice(0, 12)}</td>
                  <td>
                    <button type="button" onClick={() => { setOpen(row); }} className="text-primary-text hover:underline">
                      Details<span className="sr-only"> of record {row.id}</span>
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      {log.hasNextPage && (
        <button type="button" onClick={() => void log.fetchNextPage()} disabled={log.isFetchingNextPage} className="mt-3 btn btn-secondary">
          Load older records
        </button>
      )}
      {open !== null && (
        <Dialog title={`Record #${open.id}: ${open.action}`} onClose={() => { setOpen(null); }}>
          <pre className="max-h-[60dvh] overflow-auto rounded bg-slate-50 p-3 text-xs">{JSON.stringify(open, null, 2)}</pre>
          <div className="mt-3 text-right">
            <button type="button" onClick={() => { setOpen(null); }} className="btn btn-secondary">
              Close
            </button>
          </div>
        </Dialog>
      )}
    </div>
  );
}
