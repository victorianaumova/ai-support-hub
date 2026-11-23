import { type Ref, useState } from "react";
import { SafeText } from "../text/SafeText";
import { type Citation, type Draft, draftParts, type RejectReason } from "./useTicket";

const REJECT_REASONS: { value: RejectReason; label: string }[] = [
  { value: "wrong_facts", label: "Wrong facts" },
  { value: "wrong_tone", label: "Wrong tone" },
  { value: "missing_info", label: "Missing information" },
  { value: "policy", label: "Policy" },
  { value: "other", label: "Other" },
];

/** `[n]` markers in the text that the draft has no citation for (the API refuses them, 422). */
export function unknownMarkers(text: string, citations: Citation[]): number[] {
  const known = new Set(citations.map((c) => c.marker));
  const found = Array.from(text.matchAll(/\[(\d{1,2})\]/g), (m) => Number(m[1]));
  return [...new Set(found.filter((n) => !known.has(n)))];
}

export interface DraftActions {
  approve: (actions: string[]) => void;
  edit: (body: string, actions: string[]) => void;
  reject: (reason: RejectReason, note: string, reply: string) => void;
  busy: boolean;
}

/** UI §3 work area: editable draft, citation chips, unsupported claims, proposed actions, and
 * Approve / Send edited / Reject. "Approve and send" is off after any edit. */
export function DraftEditor({
  draft,
  editorRef,
  onCite,
  actions,
  rejectOpen,
  setRejectOpen,
}: {
  draft: Draft;
  editorRef: Ref<HTMLTextAreaElement>;
  onCite: (marker: number) => void;
  actions: DraftActions;
  rejectOpen: boolean;
  setRejectOpen: (open: boolean) => void;
}) {
  const parts = draftParts(draft);
  const [text, setText] = useState(draft.body);
  const [ticked, setTicked] = useState<string[]>([]);
  const [reason, setReason] = useState<RejectReason | "">("");
  const [note, setNote] = useState("");
  const [reply, setReply] = useState("");
  const edited = text.trim() !== draft.body.trim();
  const unknown = unknownMarkers(text, parts.citations);
  const pending = draft.status === "pending";

  return (
    <section aria-labelledby="draft-heading" className="rounded-lg border border-slate-200 bg-white p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 id="draft-heading" className="text-lg font-semibold">
          AI draft
        </h2>
        <p className="text-sm text-muted">
          Groundedness {draft.groundedness ?? "—"} · status {draft.status}
        </p>
      </div>
      {!draft.can_answer && (
        <p className="mt-2 rounded bg-amber-50 p-2 text-sm text-amber-950">
          The AI could not answer from the sources. {draft.missing_information ?? ""} Edit it into an answer, or reject it.
        </p>
      )}
      <label htmlFor="draft-text" className="mt-3 block text-sm font-medium">
        Answer to the customer {pending ? "(E to edit)" : ""}
      </label>
      <textarea
        id="draft-text"
        ref={editorRef}
        rows={8}
        maxLength={4000}
        value={text}
        readOnly={!pending}
        onChange={(event) => {
          setText(event.target.value);
        }}
        className="mt-1 w-full rounded-md border border-slate-300 p-2 font-sans"
      />
      {unknown.length > 0 && (
        <p role="alert" className="mt-1 text-sm text-red-800">
          No source for marker {unknown.map((n) => `[${n}]`).join(", ")}. Remove it or keep only the cited markers.
        </p>
      )}
      <div className="mt-3 rounded-md bg-slate-50 p-3 text-sm" aria-label="Preview with citations">
        <p className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted">Preview</p>
        <SafeText
          text={text}
          highlights={parts.claims}
          highlightTitle="The verify step found no support for this claim in the sources"
          renderMarker={(marker, key) => (
            <button
              key={key}
              type="button"
              onClick={() => {
                onCite(marker);
              }}
              aria-label={`Source ${marker}`}
              className="mx-0.5 inline-flex items-center rounded-full bg-brand-50 px-1.5 text-xs font-semibold text-brand-700 ring-1 ring-brand-600"
            >
              {marker}
            </button>
          )}
        />
      </div>
      {parts.claims.length > 0 && (
        <div className="mt-2 text-sm">
          <p className="font-medium text-red-900">Claims without support (underlined):</p>
          <ul className="list-disc pl-5">
            {parts.claims.map((claim) => <li key={claim}>{claim}</li>)}
          </ul>
        </div>
      )}

      {parts.actions.length > 0 && (
        <fieldset className="mt-4 rounded-md border border-slate-200 p-3">
          <legend className="px-1 text-sm font-semibold">Proposed actions</legend>
          {parts.actions.map((action) => (
            <div key={action.id} className="text-sm">
              <label className="flex items-start gap-2">
                <input
                  type="checkbox"
                  disabled={!pending}
                  checked={ticked.includes(action.id)}
                  onChange={(event) => {
                    setTicked((current) => (event.target.checked ? [...current, action.id] : current.filter((a) => a !== action.id)));
                  }}
                  className="mt-1"
                />
                <span>
                  <span className="font-medium">Create on send:</span> Jira {action.params.issue_type ?? ""} issue — {action.params.summary ?? ""}
                  <span className="block text-muted">{action.params.description ?? ""}</span>
                </span>
              </label>
            </div>
          ))}
        </fieldset>
      )}

      {pending && (
        <div className="mt-4 flex flex-wrap gap-2">
          <button
            type="button"
            disabled={actions.busy || edited || !draft.can_answer}
            title={edited ? "You changed the text: use Send edited" : "A"}
            onClick={() => {
              actions.approve(ticked);
            }}
            className="rounded-md bg-brand-600 px-4 py-2 font-medium text-white hover:bg-brand-700 disabled:opacity-50"
          >
            Approve and send
          </button>
          <button
            type="button"
            disabled={actions.busy || !edited || text.trim() === "" || unknown.length > 0}
            onClick={() => {
              actions.edit(text, ticked);
            }}
            className="rounded-md border border-brand-600 bg-white px-4 py-2 font-medium text-brand-700 hover:bg-brand-50 disabled:opacity-50"
          >
            Send edited
          </button>
          <button
            type="button"
            disabled={actions.busy}
            onClick={() => {
              setRejectOpen(!rejectOpen);
            }}
            aria-expanded={rejectOpen}
            className="rounded-md border border-red-400 bg-white px-4 py-2 font-medium text-red-800 hover:bg-red-50"
          >
            Reject
          </button>
        </div>
      )}
      {pending && rejectOpen && (
        <form
          className="mt-3 space-y-2 rounded-md border border-red-200 bg-red-50 p-3"
          onSubmit={(event) => {
            event.preventDefault();
            if (reason !== "") actions.reject(reason, note, reply);
          }}
        >
          <label className="block text-sm font-medium">
            Reason (required)
            <select
              required
              value={reason}
              onChange={(event) => {
                setReason(event.target.value as RejectReason);
              }}
              className="ml-2 rounded border border-slate-300 px-2 py-1"
            >
              <option value="">Choose…</option>
              {REJECT_REASONS.map((r) => <option key={r.value} value={r.value}>{r.label}</option>)}
            </select>
          </label>
          <label className="block text-sm">
            Note for the team (optional)
            <input maxLength={1000} value={note} onChange={(event) => { setNote(event.target.value); }} className="mt-1 w-full rounded border border-slate-300 px-2 py-1" />
          </label>
          <label className="block text-sm">
            Your own reply to the customer (optional; without it the ticket waits in the queue)
            <textarea maxLength={4000} rows={3} value={reply} onChange={(event) => { setReply(event.target.value); }} className="mt-1 w-full rounded border border-slate-300 px-2 py-1" />
          </label>
          <button type="submit" disabled={reason === "" || actions.busy} className="rounded-md bg-red-700 px-4 py-2 font-medium text-white disabled:opacity-50">
            Reject draft
          </button>
        </form>
      )}
      {!pending && draft.final_body !== null && <p className="mt-3 text-sm text-muted">Sent text: {draft.final_body}</p>}
    </section>
  );
}
