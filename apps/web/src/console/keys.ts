/** Keys typed into a field are text, not shortcuts. */
export function typing(target: EventTarget | null): boolean {
  return target instanceof HTMLElement && (target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName));
}

/** A plain key press that may be a console shortcut (no modifier, not in a field). */
export function shortcut(event: KeyboardEvent): string | null {
  if (event.metaKey || event.ctrlKey || event.altKey || typing(event.target)) return null;
  return event.key.toLowerCase();
}
