import { useEffect, useRef, useState } from "react";
import { describe as describeError } from "../api/problem";
import { Icon } from "../components/Icon";
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
        <button type="button" onClick={onSwitch} className="btn btn-primary mt-3">
          Choose a customer again
        </button>
      </div>
    );
  }

  return (
    <div className="mx-auto grid max-w-6xl gap-4 px-4 py-6 lg:grid-cols-[16rem_1fr]">
      <aside aria-label="Earlier conversations" className="order-2 lg:order-1">
        <div className="panel flex items-start gap-3 p-4">
          <span aria-hidden="true" className="flex size-10 shrink-0 items-center justify-center rounded-full bg-surface-hover text-muted">
            <Icon name="person" />
          </span>
          <div className="min-w-0">
          <p className="font-medium">{profile.display_name}</p>
          <p className="capitalize text-muted">{profile.tier} customer</p>
          <button type="button" onClick={onSwitch} className="mt-1 text-primary-text hover:underline">
            Switch customer
          </button>
          </div>
        </div>
        <h2 className="mt-6 px-1 text-base font-medium">Earlier conversations</h2>
        <button
          type="button"
          onClick={() => {
            open(null);
          }}
          className="btn btn-secondary mt-2 w-full"
        >
          <Icon name="add" size={18} />
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
                className={`w-full rounded-md px-3 py-2 text-left hover:bg-surface-hover ${ticket.id === state.ticketId ? "bg-primary-weak font-medium text-[var(--tb-primary-strong)] hover:bg-primary-weak" : ""}`}
              >
                {new Date(ticket.created_at).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}
                <span className="block text-xs font-normal text-muted">{ticket.status.replace("_", " ")}</span>
              </button>
            </li>
          ))}
        </ul>
      </aside>

      <section aria-label="Support chat" className="panel order-1 flex min-h-[70dvh] flex-col overflow-hidden lg:order-2">
        <header className="flex flex-wrap items-center gap-3 border-b border-line px-4 py-3">
          <span aria-hidden="true" className="flex size-8 items-center justify-center rounded-full bg-primary-weak text-[var(--tb-primary-strong)]">
            <Icon name="support_agent" size={18} />
          </span>
          <h2 className="flex-1 text-base font-medium">Tallybridge support</h2>
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
            className="btn btn-secondary"
          >
            <Icon name="person" size={18} />
            Talk to a human
          </button>
        </header>
        <p className="flex items-start gap-2 border-b border-line bg-sky-50 px-4 py-2 text-xs text-sky-950">
          <Icon name="info" size={16} />
          <span>{AI_NOTICE}</span>
        </p>

        <div className="flex-1 overflow-y-auto bg-white p-4 sm:p-6">
          {state.messages.length === 0 && !streaming && (
            <div className="py-8 text-center text-muted">
              <span aria-hidden="true" className="mx-auto flex size-12 items-center justify-center rounded-full bg-primary-weak text-[var(--tb-primary-strong)]">
                <Icon name="chat" size={24} />
              </span>
              <p className="mt-3 text-base text-ink">How can we help you today?</p>
              {suggestions.length > 0 && (
                <ul aria-label="Suggested questions" className="mt-4 flex flex-wrap justify-center gap-2">
                  {suggestions.map((text) => (
                    <li key={text}>
                      <button
                        type="button"
                        onClick={() => void send(text)}
                        className="rounded-full bg-primary-weak px-4 py-2 text-left text-[var(--tb-primary-strong)] transition-colors hover:bg-[#dddcf6]"
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
            <p role="status" className="mt-4 ml-11 flex items-center gap-2 text-muted">
              <span aria-hidden="true" className="size-2.5 animate-pulse rounded-full bg-primary" />
              {state.phase.step === null ? "Sending…" : STEPS[state.phase.step]}
            </p>
          )}
          {state.assistantDown && (
            <p role="alert" className="mt-4 flex items-start gap-2 rounded-2xl bg-amber-50 p-3 text-amber-950">
              <Icon name="warning" size={18} />
              Our assistant is not available now; a human will answer.
            </p>
          )}
          {!streaming && state.status === "pending_approval" && (
            <p role="status" className="mt-4 flex items-start gap-3 rounded-2xl bg-sky-50 p-4 text-sky-950">
              <Icon name="hourglass_top" />
              A support specialist is checking the answer before it is sent. It will appear here.
            </p>
          )}
          {!streaming && state.status === "handed_off" && !state.assistantDown && (
            <p role="status" className="mt-4 flex items-start gap-3 rounded-2xl bg-sky-50 p-4 text-sky-950">
              <Icon name="hourglass_top" />
              Waiting for a support specialist. Their reply will appear here.
            </p>
          )}
          {state.status === "closed" && (
            <p role="status" className="mt-4 flex items-start gap-3 rounded-2xl bg-surface p-4 text-muted">
              <Icon name="check_circle" />
              This conversation is closed. A new message starts a new conversation.
            </p>
          )}
          {state.phase.kind === "rate_limited" && (
            <p role="alert" className="mt-4 rounded-2xl bg-amber-50 p-3 text-amber-950">
              You are sending messages too quickly.{" "}
              {state.phase.retryAfterS !== null ? `Please try again in ${Math.ceil(state.phase.retryAfterS)} seconds.` : "Please try again in a minute."}
            </p>
          )}
          {state.phase.kind === "failed" && (
            <div role="alert" className="mt-4 rounded-2xl bg-red-50 p-3 text-red-900">
              <p>Your message could not be sent. {describeError(state.phase.error)}</p>
              <button
                type="button"
                onClick={() => {
                  if (state.phase.kind === "failed") void send(state.phase.text);
                }}
                className="btn btn-danger mt-2"
              >
                Try again
              </button>
            </div>
          )}
          {(pollError ?? humanError) !== null && (pollError ?? humanError) !== undefined && (
            <p role="alert" className="mt-4 text-red-900">
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
