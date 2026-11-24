import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";
import { api, unwrap } from "../api/client";
import { Icon } from "../components/Icon";
import { SafeText } from "../text/SafeText";
import type { Message } from "./sse";

/** Only a relative `/help/...` link is ever made clickable (UI §2 safe rendering rule). */
export function helpHref(url: string): string | null {
  return /^\/help\/[A-Za-z0-9_-]+(#[A-Za-z0-9_-]+)?$/.test(url) ? url : null;
}

function Feedback({ ticketId, messageId }: { ticketId: string; messageId: string }) {
  const [rating, setRating] = useState<"up" | "down" | null>(null);
  const [comment, setComment] = useState("");
  const give = useMutation({
    mutationFn: async (body: { rating: "up" | "down"; comment?: string }) =>
      unwrap(
        await api.POST("/api/v1/tickets/{ticket_id}/feedback", {
          params: { path: { ticket_id: ticketId } },
          body: { message_id: messageId, ...body },
        }),
      ),
  });
  if (give.isSuccess) return <p className="mt-2 text-xs text-muted">Thank you for your feedback.</p>;
  return (
    <div className="mt-2 text-xs">
      <div className="flex items-center gap-2" role="group" aria-label="Was this answer helpful?">
        <span className="text-muted">Helpful?</span>
        <button
          type="button"
          aria-pressed={rating === "up"}
          disabled={give.isPending}
          onClick={() => {
            setRating("up");
            give.mutate({ rating: "up" });
          }}
          className="inline-flex h-7 items-center gap-1 rounded-full border border-line bg-white px-3 text-muted hover:bg-surface-hover aria-pressed:border-primary aria-pressed:text-primary-text"
        >
          <Icon name="thumb_up" size={16} />
          Helpful
        </button>
        <button
          type="button"
          aria-pressed={rating === "down"}
          disabled={give.isPending}
          onClick={() => {
            setRating("down");
          }}
          className="inline-flex h-7 items-center gap-1 rounded-full border border-line bg-white px-3 text-muted hover:bg-surface-hover aria-pressed:border-primary aria-pressed:text-primary-text"
        >
          <Icon name="thumb_down" size={16} />
          Not helpful
        </button>
      </div>
      {rating === "down" && (
        <form
          className="mt-2 flex flex-wrap gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            give.mutate(comment.trim() === "" ? { rating: "down" } : { rating: "down", comment: comment.trim() });
          }}
        >
          <label htmlFor={`comment-${messageId}`} className="sr-only">
            What was missing? (optional)
          </label>
          <input
            id={`comment-${messageId}`}
            maxLength={1000}
            placeholder="What was missing? (optional)"
            value={comment}
            onChange={(event) => {
              setComment(event.target.value);
            }}
            className="field min-w-0 flex-1"
          />
          <button type="submit" disabled={give.isPending} className="btn btn-secondary">
            Send feedback
          </button>
        </form>
      )}
      {give.isError && <p role="alert" className="mt-1 text-red-800">Your feedback could not be sent. Please try again.</p>}
    </div>
  );
}

/** Who wrote a message and when, above the bubble. */
function Byline({ label, time, align }: { label: string; time: string; align: "start" | "end" }) {
  return (
    <p className={`mb-1 flex gap-2 text-xs text-muted ${align === "end" ? "justify-end" : ""}`}>
      <span className="font-medium text-ink">{label}</span>
      <span>{time}</span>
    </p>
  );
}

export function MessageItem({ message, ticketId }: { message: Message; ticketId: string | null }) {
  const time = new Date(message.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (message.role === "customer") {
    return (
      <li className="flex flex-col items-end">
        <Byline label="You" time={time} align="end" />
        <div className="max-w-[85%] rounded-2xl rounded-tr-sm bg-primary px-4 py-2 text-white">
          <span className="sr-only">You: </span>
          <SafeText text={message.body} />
        </div>
        {message.masked && (
          <p className="mt-1 flex max-w-[85%] items-center gap-1 text-xs text-amber-900">
            <Icon name="lock" size={14} />
            We hid a card number for your safety. We never need it in chat.
          </p>
        )}
      </li>
    );
  }
  if (message.role === "system") {
    return (
      <li>
        <div role="note" className="flex gap-3 rounded-lg border border-sky-300 bg-sky-50 p-4 text-sky-950">
          <Icon name="support_agent" className="mt-0.5" />
          <div className="min-w-0">
            <p className="mb-1 text-xs font-medium uppercase tracking-wide">{message.author_label}</p>
            <SafeText text={message.body} />
          </div>
        </div>
      </li>
    );
  }
  const sources = message.citations;
  return (
    <li className="flex gap-3">
      <span aria-hidden="true" className="mt-5 flex size-8 shrink-0 items-center justify-center rounded-full bg-primary-weak text-[var(--tb-primary-strong)]">
        <Icon name={message.is_ai_generated ? "smart_toy" : "support_agent"} size={18} />
      </span>
      <div className="flex min-w-0 max-w-[85%] flex-col items-start">
        <Byline label={message.author_label} time={time} align="start" />
        <div className="rounded-2xl rounded-tl-sm border border-line bg-white px-4 py-2">
          <SafeText
            text={message.body}
            renderMarker={(marker, key) => (
              <sup key={key} className="relative -top-1.5 ml-0.5 inline-block rounded-full bg-primary-weak px-1.5 py-0.5 align-baseline text-[0.6875rem] font-medium leading-none text-[var(--tb-primary-strong)]">
                [{marker}]
              </sup>
            )}
          />
          {sources.length > 0 && (
            <div className="mt-3 border-t border-line pt-2">
              <p className="text-xs font-medium text-muted">Sources</p>
              <ol className="mt-1 flex flex-wrap gap-2">
                {sources.map((source) => {
                  const href = helpHref(source.url);
                  return (
                    <li key={source.marker} className="chip max-w-full border border-line bg-white">
                      <span className="text-muted">[{source.marker}]</span>
                      {href === null ? (
                        <span className="truncate">{source.title}</span>
                      ) : (
                        <Link to={href} target="_blank" rel="noopener noreferrer" className="flex min-w-0 items-center gap-1 text-primary-text hover:underline">
                          <Icon name="description" size={14} />
                          <span className="truncate">{source.title}</span>
                        </Link>
                      )}
                    </li>
                  );
                })}
              </ol>
            </div>
          )}
        </div>
        {message.is_ai_generated && ticketId !== null && <Feedback ticketId={ticketId} messageId={message.id} />}
      </div>
    </li>
  );
}
