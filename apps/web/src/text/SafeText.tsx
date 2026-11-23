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

function inline(text: string, keyPrefix: string, renderMarker?: SafeTextProps["renderMarker"]): ReactNode[] {
  const nodes: ReactNode[] = [];
  let last = 0;
  let n = 0;
  for (const match of text.matchAll(INLINE)) {
    const index = match.index;
    if (index > last) nodes.push(text.slice(last, index));
    const key = `${keyPrefix}-${n++}`;
    const [whole, code, bold, italic, , marker] = match;
    if (code !== undefined) nodes.push(<code key={key} className="rounded bg-slate-100 px-1 font-mono text-[0.95em]">{code.slice(1, -1)}</code>);
    else if (bold !== undefined) nodes.push(<strong key={key}>{bold.slice(2, -2)}</strong>);
    else if (italic !== undefined) nodes.push(<em key={key}>{italic.slice(1, -1)}</em>);
    else if (marker !== undefined) nodes.push(renderMarker ? renderMarker(Number(marker), key) : whole);
    last = index + whole.length;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

export function SafeText({ text, renderMarker }: SafeTextProps) {
  return (
    <div className="space-y-2 break-words">
      {blocks(text).map((block, b) => {
        const key = `b${b}`;
        if (block.kind === "bullets") {
          return (
            <ul key={key} className="list-disc space-y-1 pl-5">
              {block.items.map((item, i) => <li key={i}>{inline(item, `${key}-${i}`, renderMarker)}</li>)}
            </ul>
          );
        }
        if (block.kind === "numbers") {
          return (
            <ol key={key} start={block.start} className="list-decimal space-y-1 pl-5">
              {block.items.map((item, i) => <li key={i}>{inline(item, `${key}-${i}`, renderMarker)}</li>)}
            </ol>
          );
        }
        return (
          <p key={key}>
            {block.lines.map((line, i) => (
              <span key={i}>
                {i > 0 && <br />}
                {inline(line, `${key}-${i}`, renderMarker)}
              </span>
            ))}
          </p>
        );
      })}
    </div>
  );
}
