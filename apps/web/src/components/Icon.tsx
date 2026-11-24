import { ICONS, type IconName } from "./icons.generated";

/** A Material Symbols icon (outlined, 400) drawn inline in the text colour. Decorative: the
 * text next to it carries the meaning, so it is hidden from screen readers. */
export function Icon({ name, size = 20, className = "" }: { name: IconName; size?: number; className?: string }) {
  return (
    <svg
      aria-hidden="true"
      focusable="false"
      width={size}
      height={size}
      viewBox="0 -960 960 960"
      fill="currentColor"
      className={`shrink-0 ${className}`}
    >
      <path d={ICONS[name]} />
    </svg>
  );
}
