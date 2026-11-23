import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";
import { api, unwrap } from "../api/client";
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
  if (give.isSuccess) return <p className="mt-2 text-sm text-muted">Thank you for your feedback.</p>;
  return (
    <div className="mt-2 text-sm">
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
          className="rounded border border-slate-300 px-2 py-0.5 hover:bg-slate-100"
        >
          👍 Helpful
        </button>
        <button
          type="button"
          aria-pressed={rating === "down"}
          disabled={give.isPending}
          onClick={() => {
            setRating("down");
          }}
          className="rounded border border-slate-300 px-2 py-0.5 hover:bg-slate-100"
        >
          👎 Not helpful
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
            className="min-w-0 flex-1 rounded border border-slate-300 px-2 py-1"
          />
          <button type="submit" disabled={give.isPending} className="rounded bg-slate-800 px-3 py-1 text-white">
            Send feedback
          </button>
        </form>
      )}
      {give.isError && <p role="alert" className="mt-1 text-red-800">Your feedback could not be sent. Please try again.</p>}
    </div>
  );
}

export function MessageItem({ message, ticketId }: { message: Message; ticketId: string | null }) {
  const time = new Date(message.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (message.role === "customer") {
    return (
      <li className="flex flex-col items-end">
        <div className="max-w-[85%] rounded-2xl rounded-br-sm bg-brand-600 px-4 py-2 text-white">
          <span className="sr-only">You: </span>
          <SafeText text={message.body} />
        </div>
        <span className="mt-1 text-xs text-muted">{time}</span>
        {message.masked && (
          <p className="mt-1 max-w-[85%] text-right text-xs text-amber-900">
            <span aria-hidden="true">🔒 </span>We hid a card number for your safety. We never need it in chat.
          </p>
        )}
      </li>
    );
  }
  if (message.role === "system") {
    return (
      <li>
        <div role="note" className="rounded-lg border border-sky-300 bg-sky-50 p-3 text-sky-950">
          <p className="mb-1 text-xs font-semibold uppercase tracking-wide">
            <span aria-hidden="true">ℹ </span>
            {message.author_label}
          </p>
          <SafeText text={message.body} />
        </div>
      </li>
    );
  }
  const sources = message.citations;
  return (
    <li className="flex flex-col items-start">
      <span className="mb-1 text-xs font-medium text-muted">{message.author_label}</span>
      <div className="max-w-[85%] rounded-2xl rounded-bl-sm border border-slate-200 bg-white px-4 py-2">
        <SafeText
          text={message.body}
          renderMarker={(marker, key) => (
            <sup key={key} className="ml-0.5 font-semibold text-brand-700">
              [{marker}]
            </sup>
          )}
        />
        {sources.length > 0 && (
          <div className="mt-3 border-t border-slate-100 pt-2 text-sm">
            <p className="font-medium">Sources</p>
            <ol className="mt-1 space-y-0.5">
              {sources.map((source) => {
                const href = helpHref(source.url);
                return (
                  <li key={source.marker}>
                    [{source.marker}]{" "}
                    {href === null ? (
                      <span>{source.title}</span>
                    ) : (
                      <Link to={href} target="_blank" rel="noopener noreferrer" className="text-brand-700 underline">
                        {source.title}
                      </Link>
                    )}
                  </li>
                );
              })}
            </ol>
          </div>
        )}
      </div>
      <span className="mt-1 text-xs text-muted">{time}</span>
      {message.is_ai_generated && ticketId !== null && <Feedback ticketId={ticketId} messageId={message.id} />}
    </li>
  );
}
