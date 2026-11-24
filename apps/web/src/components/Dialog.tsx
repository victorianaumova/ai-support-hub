import { type ReactNode, useEffect, useRef } from "react";

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/** A modal dialog: focus moves in and stays in (Tab wraps), Escape closes, and focus returns to
 * the element that had it when the dialog opened. */
export function Dialog({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  const box = useRef<HTMLDivElement>(null);
  // Callers pass a new function on every render. The effect below must run once per opening:
  // re-running it would move focus on every keystroke and forget where focus came from.
  const close = useRef(onClose);
  useEffect(() => {
    close.current = onClose;
  }, [onClose]);

  useEffect(() => {
    const before = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const focusable = () => Array.from(box.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? []);
    (box.current?.querySelector<HTMLElement>("input, textarea, select") ?? focusable()[0])?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        close.current();
        return;
      }
      if (event.key !== "Tab") return;
      const items = focusable();
      const first = items.at(0);
      const last = items.at(-1);
      if (first === undefined || last === undefined) {
        event.preventDefault();
        return;
      }
      const inside = box.current?.contains(document.activeElement) ?? false;
      if (event.shiftKey && (document.activeElement === first || !inside)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (document.activeElement === last || !inside)) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      before?.focus();
    };
  }, []);

  return (
    <div className="fixed inset-0 z-30 flex items-center justify-center bg-slate-900/40 p-4">
      <div ref={box} role="dialog" aria-modal="true" aria-labelledby="dialog-title" className="max-h-[90dvh] w-full max-w-lg overflow-y-auto rounded-lg border border-line bg-white p-6 shadow-[0_4px_16px_rgb(60_64_67/0.15)]">
        <h2 id="dialog-title" className="text-lg font-medium">
          {title}
        </h2>
        <div className="mt-3">{children}</div>
      </div>
    </div>
  );
}
