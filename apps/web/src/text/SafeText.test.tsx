import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { blocks, SafeText } from "./SafeText";

function rendered(text: string) {
  const { container } = render(<SafeText text={text} />);
  return container;
}

describe("SafeText: the safe rendering rule (UI §2, P10-05 cases)", () => {
  it.each([
    "<img src=x onerror=alert(1)>",
    "<script>alert(1)</script>",
    "[x](javascript:alert(1))",
    "https://evil.example/path",
    "<iframe src=https://evil.example></iframe>",
    "![img](https://evil.example/a.png)",
    "&lt;b&gt;entity&lt;/b&gt;",
  ])("shows %s as visible text with no element, link or request", (text) => {
    const container = rendered(text);
    expect(container.textContent).toBe(text);
    expect(container.querySelector("img, script, iframe, a, object, embed, video, audio, source, link")).toBeNull();
  });

  it("keeps headings, tables and quotes as literal text", () => {
    const text = "# Heading\n| a | b |\n> quote";
    const container = rendered(text);
    expect(container.querySelector("h1, table, blockquote")).toBeNull();
    expect(container.textContent).toBe("# Heading| a | b |> quote");
  });

  it("turns the allowed subset into elements", () => {
    const container = rendered("Hello **you** and *me* with `code`\nnext line\n\n- one\n- two\n\n3. three\n4. four");
    expect(container.querySelector("strong")?.textContent).toBe("you");
    expect(container.querySelector("em")?.textContent).toBe("me");
    expect(container.querySelector("code")?.textContent).toBe("code");
    expect(container.querySelectorAll("br")).toHaveLength(1);
    expect(Array.from(container.querySelectorAll("ul li"), (li) => li.textContent)).toEqual(["one", "two"]);
    expect(container.querySelector("ol")?.getAttribute("start")).toBe("3");
  });

  it("does not treat a lone asterisk as emphasis", () => {
    expect(rendered("2 * 3 = 6").textContent).toBe("2 * 3 = 6");
    expect(rendered("2 * 3 = 6").querySelector("em")).toBeNull();
  });

  it("hands citation markers to the caller", () => {
    const { container } = render(<SafeText text="It takes 1 day [1]." renderMarker={(n, key) => <sup key={key}>{n}</sup>} />);
    expect(container.querySelector("sup")?.textContent).toBe("1");
  });

  it("underlines highlighted passages as text", () => {
    const { container } = render(<SafeText text="Fees are zero. It takes **1 day**." highlights={["Fees are zero.", "<b>"]} highlightTitle="Not supported" />);
    const mark = container.querySelector("mark");
    expect(mark?.textContent).toBe("Fees are zero.");
    expect(mark?.getAttribute("title")).toBe("Not supported");
    expect(container.querySelector("strong")?.textContent).toBe("1 day");
  });

  it("splits paragraphs and lists", () => {
    expect(blocks("a\nb\n\n- c\n1. d")).toEqual([
      { kind: "paragraph", lines: ["a", "b"] },
      { kind: "bullets", items: ["c"] },
      { kind: "numbers", items: ["d"], start: 1 },
    ]);
  });
});
