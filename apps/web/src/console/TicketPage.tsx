import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router";
import { ApiError, describe } from "../api/problem";
import { useOperator } from "../auth/session";
import { ErrorState } from "../components/ErrorState";
import { Loading } from "../components/Loading";
import { SafeText } from "../text/SafeText";
import { ContextTabs } from "./ContextTabs";
import { DraftEditor } from "./DraftEditor";
import { reasonText, words } from "./labels";
import { neighbour } from "./queue";
import { type Ticket, useTicket } from "./useTicket";

/** Keys typed into a field are text, not shortcuts. */
function typing(target: EventTarget | null): boolean {
  return target instanceof HTMLElement && (target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName));
}

function actionError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === "conflict") return "Someone else changed this ticket, or it is claimed by another operator. It was reloaded.";
    if (error.code === "draft_cannot_answer") return "This draft cannot be sent unchanged: edit it or reject it.";
    if (error.code === "action_limit_reached") return "This ticket already has its Jira issue.";
    if (error.code === "ticket_closed") return "The ticket is closed.";
  }
  return describe(error);
}

function Conversation({ ticket }: { ticket: Ticket }) {
  return (
    <section aria-label="Conversation" className="rounded-lg border border-slate-200 bg-white p-3">
      <h2 className="mb-2 font-semibold">Conversation</h2>
      <ol className="space-y-3">
        {ticket.messages.map((m) => (
          <li key={m.id} className={`rounded-md p-2 text-sm ${m.role === "customer" ? "bg-slate-100" : m.role === "system" ? "border border-sky-300 bg-sky-50" : "border border-slate-200"}`}>
            <p className="text-xs text-muted">
              {m.role === "customer" ? ticket.customer.display_name : m.author_label} ·{" "}
              {new Date(m.created_at).toLocaleString([], { dateStyle: "short", timeStyle: "short" })}
            </p>
            <SafeText text={m.body} />
            {Object.keys(m.masked_entities).length > 0 && <p className="mt-1 text-xs text-amber-900">Masked: {Object.keys(m.masked_entities).join(", ")}</p>}
          </li>
        ))}
      </ol>
    </section>
  );
}

export function TicketPage() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const me = useOperator().data;
  const t = useTicket(id);
  const editor = useRef<HTMLTextAreaElement>(null);
  const [rejectOpen, setRejectOpen] = useState(false);
  const [focused, setFocused] = useState<number | null>(null);
  const [replyText, setReplyText] = useState("");
  const [issueOpen, setIssueOpen] = useState(false);
  const ticket = t.ticket.data;
  const draft = ticket?.draft ?? null;
  const reviewable = draft !== null && draft.status === "pending" && ticket?.status === "pending_approval";
  const busy = t.review.isPending || t.claim.isPending || t.release.isPending || t.close.isPending || t.reply.isPending;

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.metaKey || event.ctrlKey || event.altKey || typing(event.target)) return;
      const key = event.key.toLowerCase();
      if (key === "j" || key === "k") {
        const next = neighbour(id, key === "j" ? 1 : -1);
        if (next !== null) void navigate(`/console/tickets/${next}`);
      } else if (key === "e" && reviewable) {
        event.preventDefault();
        editor.current?.focus();
      } else if (key === "r" && reviewable) {
        setRejectOpen(true);
      } else if (key === "a" && reviewable && draft.can_answer) {
        t.review.mutate({ kind: "approve", draftId: draft.id, actions: [] });
      }
    }
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
    };
  }, [draft, id, navigate, reviewable, t.review]);

  if (t.ticket.isPending) return <Loading label="Loading the ticket" />;
  if (t.ticket.isError) return <ErrorState error={t.ticket.error} onRetry={() => void t.ticket.refetch()} />;
  const data = t.ticket.data;
  const mine = data.assigned_operator !== null && data.assigned_operator.id === me?.id;
  const canReply = data.status === "handed_off" || data.status === "answered";
  const lastError = [t.review.error, t.claim.error, t.release.error, t.close.error, t.reply.error, t.createIssue.error].find(
    (e) => e !== null && !(e instanceof ApiError && e.code === "stale_draft"),
  );

  return (
    <div className="mx-auto max-w-[96rem] px-4 py-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-xl font-semibold">
          {data.customer.display_name} · {words(data.intent)}
        </h1>
        <span className="rounded bg-slate-100 px-2 py-0.5 text-sm">{words(data.status)}</span>
        {data.urgency === "high" && <span className="rounded bg-red-100 px-2 py-0.5 text-sm font-semibold text-red-900">⚑ Urgent</span>}
        <span className="text-sm text-muted">{data.assigned_operator ? `Claimed by ${data.assigned_operator.name}` : "Not claimed"}</span>
        <div className="ml-auto flex flex-wrap gap-2">
          {data.status !== "closed" && !mine && (
            <button type="button" disabled={busy} onClick={() => { t.claim.mutate(); }} className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm">
              Claim
            </button>
          )}
          {mine && (
            <button type="button" disabled={busy} onClick={() => { t.release.mutate(); }} className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm">
              Release
            </button>
          )}
          {canReply && (
            <button type="button" disabled={busy} onClick={() => { t.close.mutate(); }} className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm">
              Close ticket
            </button>
          )}
        </div>
      </div>
      <p className="mt-1 text-xs text-muted">Shortcuts: A approve · E edit · R reject · J / K next / previous ticket</p>

      {t.stale && (
        <div role="alert" className="mt-3 flex items-center justify-between rounded-md border border-amber-400 bg-amber-50 p-3 text-amber-950">
          <span>
            <strong>The customer wrote again.</strong> The ticket was reloaded; check the new message and the new draft.
          </span>
          <button type="button" onClick={t.dismissStale} className="text-sm underline">
            Dismiss
          </button>
        </div>
      )}
      {lastError !== undefined && (
        <p role="alert" className="mt-3 rounded-md bg-red-50 p-3 text-sm text-red-900">
          {actionError(lastError)}
        </p>
      )}

      <div className="mt-4 grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)_minmax(0,1fr)]">
        <Conversation ticket={data} />
        <div className="space-y-4">
          <section aria-labelledby="why-heading" className="rounded-lg border border-slate-200 bg-white p-4">
            <h2 id="why-heading" className="font-semibold">
              Why you see this ticket
            </h2>
            <p className="mt-1 text-sm">Route: {words(data.last_route)}{data.confidence !== null ? ` · confidence ${Number(data.confidence).toFixed(2)}` : ""}</p>
            <ul className="mt-2 flex flex-wrap gap-1">
              {data.route_reasons.map((code) => (
                <li key={code} className="rounded bg-slate-100 px-2 py-0.5 text-sm">
                  {reasonText(code, data.confidence)}
                </li>
              ))}
            </ul>
            {data.summary && (
              <div className="mt-2 text-sm">
                <p className="font-medium">AI summary</p>
                <SafeText text={data.summary} />
              </div>
            )}
          </section>

          {draft !== null && (
            <DraftEditor
              key={`${draft.id}-${data.version}`}
              draft={draft}
              editorRef={editor}
              onCite={(marker) => {
                setFocused(marker);
                document.getElementById(`source-${marker}`)?.scrollIntoView({ block: "nearest" });
              }}
              rejectOpen={rejectOpen}
              setRejectOpen={setRejectOpen}
              actions={{
                busy,
                approve: (actions) => { t.review.mutate({ kind: "approve", draftId: draft.id, actions }); },
                edit: (body, actions) => { t.review.mutate({ kind: "edit", draftId: draft.id, body, actions }); },
                reject: (reason, note, reply) => {
                  t.review.mutate({ kind: "reject", draftId: draft.id, reason, note, reply });
                  setRejectOpen(false);
                },
              }}
            />
          )}

          {canReply && (
            <section aria-labelledby="reply-heading" className="rounded-lg border border-slate-200 bg-white p-4">
              <h2 id="reply-heading" className="font-semibold">
                Reply to the customer
              </h2>
              <form
                onSubmit={(event) => {
                  event.preventDefault();
                  if (replyText.trim() === "") return;
                  t.reply.mutate(replyText.trim(), { onSuccess: () => { setReplyText(""); } });
                }}
              >
                <label htmlFor="reply" className="sr-only">
                  Your reply
                </label>
                <textarea id="reply" rows={4} maxLength={4000} value={replyText} onChange={(e) => { setReplyText(e.target.value); }} className="mt-2 w-full rounded-md border border-slate-300 p-2" />
                <div className="mt-2 flex flex-wrap gap-2">
                  <button type="submit" disabled={busy || replyText.trim() === ""} className="rounded-md bg-brand-600 px-4 py-2 font-medium text-white disabled:opacity-50">
                    Send reply
                  </button>
                  <button type="button" onClick={() => { setIssueOpen(!issueOpen); }} aria-expanded={issueOpen} className="rounded-md border border-slate-300 px-3 py-2 text-sm">
                    Create Jira issue…
                  </button>
                </div>
              </form>
              {issueOpen && (
                <form
                  className="mt-3 space-y-2 rounded-md border border-slate-200 p-3 text-sm"
                  onSubmit={(event) => {
                    event.preventDefault();
                    const form = new FormData(event.currentTarget);
                    t.createIssue.mutate(
                      {
                        issue_type: form.get("issue_type") as "dispute" | "fraud" | "bug" | "limit_request",
                        summary: (form.get("summary") as string | null) ?? "",
                        description: (form.get("description") as string | null) ?? "",
                      },
                      { onSuccess: () => { setIssueOpen(false); } },
                    );
                  }}
                >
                  <label className="block">
                    Type
                    <select name="issue_type" required className="ml-2 rounded border border-slate-300 px-2 py-1">
                      <option value="dispute">dispute</option>
                      <option value="fraud">fraud</option>
                      <option value="bug">bug</option>
                      <option value="limit_request">limit request</option>
                    </select>
                  </label>
                  <label className="block">
                    Summary
                    <input name="summary" required maxLength={255} className="mt-1 w-full rounded border border-slate-300 px-2 py-1" />
                  </label>
                  <label className="block">
                    Description
                    <textarea name="description" required maxLength={5000} rows={3} className="mt-1 w-full rounded border border-slate-300 px-2 py-1" />
                  </label>
                  <button type="submit" disabled={t.createIssue.isPending} className="rounded-md bg-slate-800 px-3 py-1.5 text-white">
                    Create issue
                  </button>
                </form>
              )}
            </section>
          )}
        </div>
        <ContextTabs ticket={data} focused={focused} />
      </div>
    </div>
  );
}
