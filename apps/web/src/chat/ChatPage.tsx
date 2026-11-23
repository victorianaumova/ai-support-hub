import { useEffect, useRef, useState } from "react";
import { describe as describeError } from "../api/problem";
import { Composer } from "./Composer";
import { MessageItem } from "./MessageItem";
import { Picker } from "./Picker";
import { type DemoCustomer, loadProfile, type Profile, saveProfile } from "./profile";
import type { Step } from "./sse";
import { useChat } from "./useChat";

/** The fixed AI notice (security §7). */
export const AI_NOTICE = "You are chatting with Tallybridge's AI assistant. It can make mistakes. You can ask for a human at any time.";

const STEPS: Record<Step, string> = {
  understanding: "Understanding your question…",
  searching: "Checking the help centre and your account…",
  writing: "Writing the answer…",
  checking: "Checking the answer…",
};

const SUGGESTIONS_KEY = "hub.chat.suggestions";

function loadSuggestions(): string[] {
  try {
    const raw = sessionStorage.getItem(SUGGESTIONS_KEY);
    return raw === null ? [] : (JSON.parse(raw) as string[]);
  } catch {
    return [];
  }
}

export function ChatPage() {
  const [profile, setProfile] = useState<Profile | null>(loadProfile);
  const [suggestions, setSuggestions] = useState<string[]>(loadSuggestions);
  if (profile === null) {
    return (
      <Picker
        onChosen={(chosen: Profile, customer: DemoCustomer) => {
          saveProfile(chosen);
          try {
            sessionStorage.setItem(SUGGESTIONS_KEY, JSON.stringify(customer.suggestions));
          } catch {
            // storage blocked: the chips just do not survive a reload
          }
          setSuggestions(customer.suggestions);
          setProfile(chosen);
        }}
      />
    );
  }
  return (
    <Chat
      profile={profile}
      suggestions={suggestions}
      onSwitch={() => {
        saveProfile(null);
        setProfile(null);
      }}
    />
  );
}

function Chat({ profile, suggestions, onSwitch }: { profile: Profile; suggestions: string[]; onSwitch: () => void }) {
  const { state, send, open, requestHuman, tickets, pollError } = useChat();
  const [humanError, setHumanError] = useState<unknown>(null);
  const end = useRef<HTMLDivElement>(null);
  const streaming = state.phase.kind === "streaming";
  const waiting = state.status === "pending_approval" || state.status === "handed_off" || state.status === "processing";

  useEffect(() => {
    end.current?.scrollIntoView({ block: "end" });
  }, [state.messages.length, state.phase.kind]);

  // The session cookie ended (8 hours) or the customer was erased: pick again.
  if (tickets.error && describeError(tickets.error).startsWith("Your session has ended")) {
    return (
      <div className="mx-auto max-w-xl p-6" role="alert">
        <p>Your demo session has ended.</p>
        <button type="button" onClick={onSwitch} className="mt-3 rounded-md bg-brand-600 px-4 py-2 text-white">
          Choose a customer again
        </button>
      </div>
    );
  }

  return (
    <div className="mx-auto grid max-w-6xl gap-4 px-2 py-4 sm:px-4 lg:grid-cols-[16rem_1fr]">
      <aside aria-label="Earlier conversations" className="order-2 lg:order-1">
        <div className="rounded-lg border border-slate-200 bg-white p-3">
          <p className="font-medium">{profile.display_name}</p>
          <p className="text-sm capitalize text-muted">{profile.tier} customer</p>
          <button type="button" onClick={onSwitch} className="mt-2 text-sm text-brand-700 underline">
            Switch customer
          </button>
        </div>
        <h2 className="mt-4 text-sm font-semibold">Earlier conversations</h2>
        <button
          type="button"
          onClick={() => {
            open(null);
          }}
          className="mt-2 w-full rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm hover:bg-slate-100"
        >
          New conversation
        </button>
        <ul className="mt-2 space-y-1">
          {(tickets.data ?? []).map((ticket) => (
            <li key={ticket.id}>
              <button
                type="button"
                aria-current={ticket.id === state.ticketId ? "true" : undefined}
                onClick={() => {
                  open(ticket.id);
                }}
                className={`w-full rounded-md px-3 py-1.5 text-left text-sm hover:bg-slate-100 ${ticket.id === state.ticketId ? "bg-brand-50 font-semibold" : ""}`}
              >
                {new Date(ticket.created_at).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}
                <span className="block text-xs text-muted">{ticket.status.replace("_", " ")}</span>
              </button>
            </li>
          ))}
        </ul>
      </aside>

      <section aria-label="Support chat" className="order-1 flex min-h-[70dvh] flex-col overflow-hidden rounded-lg border border-slate-200 bg-slate-50 lg:order-2">
        <header className="flex flex-wrap items-center gap-3 border-b border-slate-200 bg-white p-3">
          <p className="flex-1 text-sm">
            <span aria-hidden="true">🤖 </span>
            {AI_NOTICE}
          </p>
          <button
            type="button"
            disabled={state.ticketId === null || waiting || state.status === "closed"}
            title={state.ticketId === null ? "Send a message first" : undefined}
            onClick={() => {
              setHumanError(null);
              requestHuman().catch((error: unknown) => {
                setHumanError(error);
              });
            }}
            className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium hover:bg-slate-100 disabled:opacity-60"
          >
            Talk to a human
          </button>
        </header>

        <div className="flex-1 overflow-y-auto p-4">
          {state.messages.length === 0 && !streaming && (
            <div className="text-center text-muted">
              <p>How can we help you today?</p>
              {suggestions.length > 0 && (
                <ul aria-label="Suggested questions" className="mt-4 flex flex-wrap justify-center gap-2">
                  {suggestions.map((text) => (
                    <li key={text}>
                      <button
                        type="button"
                        onClick={() => void send(text)}
                        className="rounded-full border border-brand-600 bg-white px-3 py-1.5 text-left text-sm text-brand-700 hover:bg-brand-50"
                      >
                        {text}
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
          <ol aria-label="Messages" aria-live="polite" className="space-y-4">
            {state.messages.map((message) => (
              <MessageItem key={message.id} message={message} ticketId={state.ticketId} />
            ))}
          </ol>

          {state.phase.kind === "streaming" && (
            <p role="status" className="mt-4 flex items-center gap-2 text-sm text-muted">
              <span aria-hidden="true" className="size-3 animate-pulse rounded-full bg-brand-600" />
              {state.phase.step === null ? "Sending…" : STEPS[state.phase.step]}
            </p>
          )}
          {state.assistantDown && (
            <p role="alert" className="mt-4 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950">
              Our assistant is not available now; a human will answer.
            </p>
          )}
          {!streaming && state.status === "pending_approval" && (
            <p role="status" className="mt-4 rounded-md bg-white p-3 text-sm text-muted">
              A support specialist is checking the answer before it is sent. It will appear here.
            </p>
          )}
          {!streaming && state.status === "handed_off" && !state.assistantDown && (
            <p role="status" className="mt-4 rounded-md bg-white p-3 text-sm text-muted">
              Waiting for a support specialist. Their reply will appear here.
            </p>
          )}
          {state.status === "closed" && (
            <p role="status" className="mt-4 rounded-md bg-white p-3 text-sm text-muted">
              This conversation is closed. A new message starts a new conversation.
            </p>
          )}
          {state.phase.kind === "rate_limited" && (
            <p role="alert" className="mt-4 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950">
              You are sending messages too quickly.{" "}
              {state.phase.retryAfterS !== null ? `Please try again in ${Math.ceil(state.phase.retryAfterS)} seconds.` : "Please try again in a minute."}
            </p>
          )}
          {state.phase.kind === "failed" && (
            <div role="alert" className="mt-4 rounded-md border border-red-300 bg-red-50 p-3 text-sm text-red-900">
              <p>Your message could not be sent. {describeError(state.phase.error)}</p>
              <button
                type="button"
                onClick={() => {
                  if (state.phase.kind === "failed") void send(state.phase.text);
                }}
                className="mt-2 rounded border border-red-400 bg-white px-3 py-1"
              >
                Try again
              </button>
            </div>
          )}
          {(pollError ?? humanError) !== null && (pollError ?? humanError) !== undefined && (
            <p role="alert" className="mt-4 text-sm text-red-900">
              {describeError(pollError ?? humanError)}
            </p>
          )}
          <div ref={end} />
        </div>

        <Composer disabled={streaming} onSend={(text) => void send(text)} />
      </section>
    </div>
  );
}
