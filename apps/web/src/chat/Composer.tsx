import { type KeyboardEvent, useState } from "react";

export const MAX_CHARS = 4000;
export const COUNTER_FROM = 3500;

/** Input of UI §2: 4,000 characters, a counter from 3,500, Enter sends, Shift+Enter breaks. */
export function Composer({ disabled, onSend }: { disabled: boolean; onSend: (text: string) => void }) {
  const [text, setText] = useState("");
  const trimmed = text.trim();
  const send = () => {
    if (disabled || trimmed === "") return;
    onSend(trimmed);
    setText("");
  };
  const keyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      send();
    }
  };
  return (
    <form
      className="border-t border-slate-200 bg-white p-3"
      onSubmit={(event) => {
        event.preventDefault();
        send();
      }}
    >
      <label htmlFor="message" className="sr-only">
        Your message
      </label>
      <div className="flex items-end gap-2">
        <textarea
          id="message"
          rows={2}
          maxLength={MAX_CHARS}
          value={text}
          disabled={disabled}
          placeholder={disabled ? "Please wait for the answer…" : "Write your message"}
          onChange={(event) => {
            setText(event.target.value);
          }}
          onKeyDown={keyDown}
          aria-describedby="message-help"
          className="min-h-[3rem] flex-1 resize-y rounded-md border border-slate-300 px-3 py-2 disabled:bg-slate-50"
        />
        <button
          type="submit"
          disabled={disabled || trimmed === ""}
          className="rounded-md bg-brand-600 px-4 py-2 font-medium text-white hover:bg-brand-700 disabled:opacity-60"
        >
          Send
        </button>
      </div>
      <p id="message-help" className="mt-1 flex justify-between text-xs text-muted">
        <span>Enter sends · Shift+Enter adds a line</span>
        {text.length >= COUNTER_FROM && (
          <span aria-live="polite" className={text.length >= MAX_CHARS ? "font-semibold text-red-800" : ""}>
            {text.length} / {MAX_CHARS}
          </span>
        )}
      </p>
    </form>
  );
}
