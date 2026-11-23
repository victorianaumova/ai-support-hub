import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { renderApp } from "../test/renderApp";

function serve(status: number, body: unknown) {
  vi.stubGlobal("fetch", () => Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } })));
}

describe("help article", () => {
  it("shows the sections with their anchors and focuses the linked one", async () => {
    serve(200, {
      id: "withdrawal-times",
      title: "How long does a withdrawal take?",
      url: "/help/withdrawal-times",
      updated_at: "2026-11-01T00:00:00Z",
      sections: [
        { anchor: "sepa-withdrawals", heading: "SEPA withdrawals", text: "One business day. <b>not bold</b>" },
        { anchor: "swift", heading: "SWIFT", text: "Longer." },
      ],
    });
    renderApp("/help/withdrawal-times#sepa-withdrawals");

    expect(await screen.findByRole("heading", { name: "How long does a withdrawal take?" })).toBeInTheDocument();
    const section = document.getElementById("sepa-withdrawals");
    expect(section).toHaveTextContent("One business day. <b>not bold</b>");
    expect(section?.querySelector("b")).toBeNull();
    expect(section).toHaveFocus();
  });

  it("says when an article does not exist (internal articles answer 404 too)", async () => {
    serve(404, { status: 404, code: "not_found", title: "Not Found", detail: "" });
    renderApp("/help/internal-thing");
    expect(await screen.findByRole("heading", { name: "Article not found" })).toBeInTheDocument();
  });
});
