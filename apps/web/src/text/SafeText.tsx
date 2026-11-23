import type { ReactNode } from "react";

/**
 * The safe rendering rule (UI §2, security T13): text that a customer, an operator or a model
 * wrote is shown as React text nodes. Only a small Markdown subset becomes elements:
 * paragraphs, line breaks, `**bold**`, `*italic*`, bulleted and numbered lists, `inline code`.
 * Everything else — headings, tables, quotes, raw HTML, entities, links, images — stays literal
 * text. There is no HTML string anywhere, so nothing in the text can create an element, a link
 * or a request.
 *
 * `renderMarker` lets the chat turn citation markers like `[1]` into something of its own
 * (still built from the text, never from HTML).
 */
export interface SafeTextProps {
  text: string;
  renderMarker?: (marker: number, key: string) => ReactNode;
  /** Exact passages to underline (unsupported claims of the verify step, UI §3). */
  highlights?: readonly string[];
  highlightTitle?: string;
}

interface Inline {
  renderMarker?: SafeTextProps["renderMarker"] | undefined;
  highlights: readonly string[];
  title: string;
}

/** Plain text with the highlighted passages wrapped; still only text nodes inside. */
function plain(text: string, key: string, options: Inline): ReactNode[] {
  const wanted = options.highlights.filter((h) => h.trim() !== "" && text.includes(h));
  if (wanted.length === 0) return [text];
  const nodes: ReactNode[] = [];
  let rest = text;
  let n = 0;
  for (;;) {
    let best: { index: number; passage: string } | null = null;
    for (const passage of wanted) {
      const index = rest.indexOf(passage);
      if (index !== -1 && (best === null || index < best.index)) best = { index, passage };
    }
    if (best === null) break;
    if (best.index > 0) nodes.push(rest.slice(0, best.index));
    nodes.push(
      <mark key={`${key}-h${n++}`} title={options.title} className="bg-transparent underline decoration-red-700 decoration-wavy decoration-2">
        {best.passage}
      </mark>,
    );
    rest = rest.slice(best.index + best.passage.length);
  }
  if (rest !== "") nodes.push(rest);
  return nodes;
}

type Block =
  | { kind: "paragraph"; lines: string[] }
  | { kind: "bullets"; items: string[] }
  | { kind: "numbers"; items: string[]; start: number };

const BULLET = /^\s*[-*•]\s+(.*)$/;
const NUMBER = /^\s*(\d{1,3})[.)]\s+(.*)$/;

/** Splits text into paragraphs and lists. Pure, so tests can check it without React. */
export function blocks(text: string): Block[] {
  const result: Block[] = [];
  let current: Block | null = null;
  const flush = () => {
    if (current !== null) result.push(current);
    current = null;
  };
  for (const line of text.replace(/\r\n?/g, "\n").split("\n")) {
    if (line.trim() === "") {
      flush();
      continue;
    }
    const bullet = BULLET.exec(line);
    const number = bullet ? null : NUMBER.exec(line);
    if (bullet) {
      if (current?.kind !== "bullets") {
        flush();
        current = { kind: "bullets", items: [] };
      }
      current.items.push(bullet[1] ?? "");
    } else if (number) {
      if (current?.kind !== "numbers") {
        flush();
        current = { kind: "numbers", items: [], start: Number(number[1]) };
      }
      current.items.push(number[2] ?? "");
    } else {
      if (current?.kind !== "paragraph") {
        flush();
        current = { kind: "paragraph", lines: [] };
      }
      current.lines.push(line);
    }
  }
  flush();
  return result;
}

// `code`, **bold**, *italic*, [n] citation markers — in this order of precedence.
const INLINE = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(\*[^*\s][^*\n]*\*)|(\[(\d{1,2})\])/g;

function inline(text: string, keyPrefix: string, options: Inline): ReactNode[] {
  const { renderMarker } = options;
  const nodes: ReactNode[] = [];
  let last = 0;
  let n = 0;
  for (const match of text.matchAll(INLINE)) {
    const index = match.index;
    if (index > last) nodes.push(...plain(text.slice(last, index), `${keyPrefix}-p${n}`, options));
    const key = `${keyPrefix}-${n++}`;
    const [whole, code, bold, italic, , marker] = match;
    if (code !== undefined) nodes.push(<code key={key} className="rounded bg-slate-100 px-1 font-mono text-[0.95em]">{code.slice(1, -1)}</code>);
    else if (bold !== undefined) nodes.push(<strong key={key}>{bold.slice(2, -2)}</strong>);
    else if (italic !== undefined) nodes.push(<em key={key}>{italic.slice(1, -1)}</em>);
    else if (marker !== undefined) nodes.push(renderMarker ? renderMarker(Number(marker), key) : whole);
    last = index + whole.length;
  }
  if (last < text.length) nodes.push(...plain(text.slice(last), `${keyPrefix}-end`, options));
  return nodes;
}

export function SafeText({ text, renderMarker, highlights = [], highlightTitle = "" }: SafeTextProps) {
  const options: Inline = { renderMarker, highlights, title: highlightTitle };
  return (
    <div className="space-y-2 break-words">
      {blocks(text).map((block, b) => {
        const key = `b${b}`;
        if (block.kind === "bullets") {
          return (
            <ul key={key} className="list-disc space-y-1 pl-5">
              {block.items.map((item, i) => <li key={i}>{inline(item, `${key}-${i}`, options)}</li>)}
            </ul>
          );
        }
        if (block.kind === "numbers") {
          return (
            <ol key={key} start={block.start} className="list-decimal space-y-1 pl-5">
              {block.items.map((item, i) => <li key={i}>{inline(item, `${key}-${i}`, options)}</li>)}
            </ol>
          );
        }
        return (
          <p key={key}>
            {block.lines.map((line, i) => (
              <span key={i}>
                {i > 0 && <br />}
                {inline(line, `${key}-${i}`, options)}
              </span>
            ))}
          </p>
        );
      })}
    </div>
  );
}
