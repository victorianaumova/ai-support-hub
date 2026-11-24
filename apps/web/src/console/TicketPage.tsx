import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { ApiError, describe } from "../api/problem";
import { useOperator } from "../auth/session";
import { ErrorState } from "../components/ErrorState";
import { Icon } from "../components/Icon";
import { Loading } from "../components/Loading";
import { SafeText } from "../text/SafeText";
import { ContextTabs } from "./ContextTabs";
import { DraftEditor } from "./DraftEditor";
import { shortcut } from "./keys";
import { reasonText, reviewRuleText, words } from "./labels";
import { neighbour } from "./queue";
import { isUrgent } from "./sla";
import { type Ticket, useTicket } from "./useTicket";

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
    <section aria-label="Conversation" className="panel self-start p-4">
      <h2 className="mb-3 text-base font-medium">Conversation</h2>
      <ol className="space-y-3">
        {ticket.messages.map((m) => (
          <li key={m.id} className={`rounded-lg p-3 ${m.role === "customer" ? "bg-surface-hover" : m.role === "system" ? "border border-sky-300 bg-sky-50" : "border border-line"}`}>
            <p className="mb-1 text-xs text-muted">
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
    // `A` lives in DraftEditor, which holds the edited text and the ticked actions.
    function onKey(event: KeyboardEvent) {
      const key = shortcut(event);
      if (key === "j" || key === "k") {
        const next = neighbour(id, key === "j" ? 1 : -1);
        if (next !== null) void navigate(`/console/tickets/${next}`);
      } else if (key === "e" && reviewable) {
        event.preventDefault();
        editor.current?.focus();
      } else if (key === "r" && reviewable) {
        setRejectOpen(true);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
    };
  }, [id, navigate, reviewable]);

  if (t.ticket.isPending) return <Loading label="Loading the ticket" />;
  if (t.ticket.isError) return <ErrorState error={t.ticket.error} onRetry={() => void t.ticket.refetch()} />;
  const data = t.ticket.data;
  const mine = data.assigned_operator !== null && data.assigned_operator.id === me?.id;
  const canReply = data.status === "handed_off" || data.status === "answered";
  const lastError = [t.review.error, t.claim.error, t.release.error, t.close.error, t.reply.error, t.createIssue.error].find(
    (e) => e !== null && !(e instanceof ApiError && e.code === "stale_draft"),
  );

  return (
    <div className="px-4 py-5 sm:px-8 md:py-8 lg:px-10">
      <div className="flex min-h-12 flex-wrap items-center gap-3">
        <Link to="/console" aria-label="Back to the queue" className="btn-icon -ml-2">
          <Icon name="arrow_back" />
        </Link>
        <h1 className="page-title">
          {data.customer.display_name} · {words(data.intent)}
        </h1>
        <span className="chip chip-info capitalize">{words(data.status)}</span>
        {isUrgent(data) && (
          <span className="chip chip-danger">
            <Icon name="flag" size={14} />
            Urgent
          </span>
        )}
        <span className="text-muted">{data.assigned_operator ? `Claimed by ${data.assigned_operator.name}` : "Not claimed"}</span>
        <div className="ml-auto flex flex-wrap gap-2">
          {data.status !== "closed" && !mine && (
            <button type="button" disabled={busy} onClick={() => { t.claim.mutate(); }} className="btn btn-secondary">
              Claim
            </button>
          )}
          {mine && (
            <button type="button" disabled={busy} onClick={() => { t.release.mutate(); }} className="btn btn-secondary">
              Release
            </button>
          )}
          {canReply && (
            <button type="button" disabled={busy} onClick={() => { t.close.mutate(); }} className="btn btn-secondary">
              Close ticket
            </button>
          )}
        </div>
      </div>
      <p className="mt-1 pl-10 text-xs text-muted">Shortcuts: A approve · E edit · R reject · J / K next / previous ticket</p>

      {t.stale && (
        <div role="alert" className="mt-4 flex items-center justify-between gap-3 rounded-lg border border-amber-400 bg-amber-50 p-3 text-amber-950">
          <span>
            <strong>The customer wrote again.</strong> The ticket was reloaded; check the new message and the new draft.
          </span>
          <button type="button" onClick={t.dismissStale} className="btn btn-secondary">
            Dismiss
          </button>
        </div>
      )}
      {lastError !== undefined && (
        <p role="alert" className="mt-4 rounded-lg border border-red-300 bg-red-50 p-3 text-red-900">
          {actionError(lastError)}
        </p>
      )}

      <div className="mt-4 grid gap-4 border-t border-line pt-4 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)_minmax(0,1fr)]">
        <Conversation ticket={data} />
        <div className="space-y-4">
          <section aria-labelledby="why-heading" className="panel p-4">
            <h2 id="why-heading" className="text-base font-medium">
              Why you see this ticket
            </h2>
            <p className="mt-1">Route: {words(data.last_route)}{data.confidence !== null ? ` · confidence ${Number(data.confidence).toFixed(2)}` : ""}</p>
            <ul className="mt-2 flex flex-wrap gap-1">
              {data.route_reasons.map((code) => (
                <li key={code} className="chip chip-info whitespace-normal py-1">
                  {reasonText(code, data.confidence)}
                </li>
              ))}
            </ul>
            {data.review && (
              <div className="mt-3">
                <p className="font-medium">Reviewed because</p>
                {data.review.rules.length === 0 ? (
                  <p className="text-muted">The matching rule is not recorded for this ticket.</p>
                ) : (
                  <ul className="list-disc pl-5">
                    {data.review.rules.map((rule) => (
                      <li key={rule}>{reviewRuleText(rule)}</li>
                    ))}
                  </ul>
                )}
                {data.review.repeat_of.length > 0 && (
                  <>
                    <p className="mt-1">Earlier tickets on this topic:</p>
                    <ul className="list-disc pl-5">
                      {data.review.repeat_of.map((other) => (
                        <li key={other.id}>
                          <Link to={`/console/tickets/${other.id}`} className="text-primary-text hover:underline">
                            {words(other.intent)} · {words(other.status)} · {new Date(other.created_at).toLocaleDateString()}
                          </Link>
                        </li>
                      ))}
                    </ul>
                  </>
                )}
              </div>
            )}
            {data.summary && (
              <div className="mt-3">
                <p className="font-medium">AI summary</p>
                <SafeText text={data.summary} />
              </div>
            )}
          </section>

          {draft !== null && (
            <DraftEditor
              key={`${draft.id}-${data.version}`}
              draft={draft}
              reviewable={reviewable}
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
            <section aria-labelledby="reply-heading" className="panel p-4">
              <h2 id="reply-heading" className="text-base font-medium">
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
                <textarea id="reply" rows={4} maxLength={4000} value={replyText} onChange={(e) => { setReplyText(e.target.value); }} className="field mt-2 w-full p-2" />
                <div className="mt-2 flex flex-wrap gap-2">
                  <button type="submit" disabled={busy || replyText.trim() === ""} className="btn btn-primary">
                    Send reply
                  </button>
                  <button type="button" onClick={() => { setIssueOpen(!issueOpen); }} aria-expanded={issueOpen} className="btn btn-secondary">
                    Create Jira issue…
                  </button>
                </div>
              </form>
              {issueOpen && (
                <form
                  className="mt-3 space-y-2 rounded-lg border border-line p-3"
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
                    <select name="issue_type" required className="field ml-2">
                      <option value="dispute">dispute</option>
                      <option value="fraud">fraud</option>
                      <option value="bug">bug</option>
                      <option value="limit_request">limit request</option>
                    </select>
                  </label>
                  <label className="block">
                    Summary
                    <input name="summary" required maxLength={255} className="field mt-1 w-full" />
                  </label>
                  <label className="block">
                    Description
                    <textarea name="description" required maxLength={5000} rows={3} className="field mt-1 w-full py-1" />
                  </label>
                  <button type="submit" disabled={t.createIssue.isPending} className="btn btn-primary">
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
