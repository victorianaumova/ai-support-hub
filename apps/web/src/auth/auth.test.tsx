import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { setCsrfToken } from "../api/client";
import { safeNext } from "../pages/ConsoleLogin";
import { fakeApi, LEAD, OPERATOR } from "../test/fakeApi";
import { renderApp } from "../test/renderApp";

const NO_SESSION = { status: 401, body: { status: 401, code: "unauthorized", title: "Unauthorized", detail: "" } };

describe("operator login", () => {
  it("sends a visitor to the login page and back to the queue after signing in", async () => {
    setCsrfToken(null);
    const calls = fakeApi({
      "GET /api/v1/auth/me": () => NO_SESSION,
      "POST /api/v1/auth/login": () => ({ status: 200, body: { operator: OPERATOR, csrf_token: "csrf-1" } }),
      "POST /api/v1/auth/logout": () => ({ status: 204 }),
    });
    const router = renderApp("/console");

    await screen.findByRole("heading", { name: "Operator login" });
    expect(router.state.location.search).toBe("?next=%2Fconsole");
    await userEvent.type(screen.getByLabelText("Email"), OPERATOR.email);
    await userEvent.type(screen.getByLabelText("Password"), "secret");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await screen.findByRole("heading", { name: "Ticket queue" });
    expect(calls.find((c) => c.path === "/api/v1/auth/login")?.body).toEqual({ email: OPERATOR.email, password: "secret" });
    const nav = screen.getByRole("navigation", { name: "Console" });
    expect(nav).toHaveTextContent("Queue");
    expect(nav).not.toHaveTextContent("Settings");

    await userEvent.click(screen.getByRole("button", { name: "Log out" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/console/login");
    });
    const logout = calls.find((c) => c.path === "/api/v1/auth/logout");
    expect(logout?.headers.get("X-CSRF-Token")).toBe("csrf-1");
  });

  it("shows a plain message for wrong credentials and never sends the token on reads", async () => {
    setCsrfToken("old");
    const calls = fakeApi({
      "GET /api/v1/auth/me": () => NO_SESSION,
      "POST /api/v1/auth/login": () => ({ status: 401, body: { status: 401, code: "invalid_credentials", title: "Unauthorized", detail: "x" } }),
    });
    renderApp("/console/login");

    await userEvent.type(await screen.findByLabelText("Email"), "a@b.test");
    await userEvent.type(screen.getByLabelText("Password"), "wrong");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Email or password is not correct.");
    expect(calls.filter((c) => c.method === "GET").every((c) => !c.headers.has("X-CSRF-Token"))).toBe(true);
  });

  it("gets the CSRF token again from /auth/me after a reload", async () => {
    setCsrfToken(null);
    const calls = fakeApi({
      "GET /api/v1/auth/me": () => ({ status: 200, body: { operator: LEAD, csrf_token: "csrf-2" } }),
      "POST /api/v1/auth/logout": () => ({ status: 204 }),
    });
    renderApp("/console/settings");

    await screen.findByRole("heading", { name: "Settings" });
    await userEvent.click(screen.getByRole("button", { name: "Log out" }));
    await waitFor(() => {
      expect(calls.some((c) => c.path === "/api/v1/auth/logout")).toBe(true);
    });
    expect(calls.find((c) => c.path === "/api/v1/auth/logout")?.headers.get("X-CSRF-Token")).toBe("csrf-2");
  });
});

describe("role guard", () => {
  it("tells an operator that the settings page is not for their role", async () => {
    fakeApi({ "GET /api/v1/auth/me": () => ({ status: 200, body: { operator: OPERATOR, csrf_token: "t" } }) });
    renderApp("/console/settings");

    expect(await screen.findByRole("alert")).toHaveTextContent("Your role (operator) cannot open this page.");
  });

  it("shows an error with the request id when the session check fails", async () => {
    fakeApi({ "GET /api/v1/auth/me": () => ({ status: 500, body: { status: 500, code: "internal_error", title: "x", detail: "x" } }) });
    renderApp("/console");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Something went wrong on our side.");
    expect(alert).toHaveTextContent("Reference: testrequestid");
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });
});

describe("safeNext", () => {
  it.each([
    [null, "/console"],
    ["/console/audit?x=1", "/console/audit?x=1"],
    ["https://evil.example", "/console"],
    ["//evil.example/console", "/console"],
    ["/chat", "/console"],
  ])("%s → %s", (value, expected) => {
    expect(safeNext(value)).toBe(expected);
  });
});
