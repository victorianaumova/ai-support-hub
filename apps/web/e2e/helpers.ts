import AxeBuilder from "@axe-core/playwright";
import { type APIRequestContext, expect, type Page } from "@playwright/test";

export const ACCESS_CODE = "e2e-access-code";
export const PASSWORD = "e2e-operator-password";
export const OPERATOR = "avery.lindgren@tallybridge.example";
export const LEAD = "noor.haddad@tallybridge.example";
/** The SEPA ticket with a recorded model answer (apps/api/tests/fixtures/run/tickets.jsonl). */
export const SEPA =
  "Hi, I withdrew 420 EUR to my bank account by SEPA yesterday afternoon and it is still not there. How long does a SEPA withdrawal take?";

export async function noAxeViolations(page: Page): Promise<void> {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  expect(results.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`)).toEqual([]);
}

export async function customer(page: Page, name = "Lucia Ferrandis"): Promise<void> {
  await page.goto("/chat");
  await page.getByLabel("Access code").fill(ACCESS_CODE);
  await page.getByRole("button", { name: "Continue" }).click();
  await page.getByRole("button", { name: new RegExp(name) }).click();
  await expect(page.getByText("You are chatting with Tallybridge's AI assistant.")).toBeVisible();
}

export async function send(page: Page, text: string): Promise<void> {
  const box = page.getByLabel("Your message");
  await expect(box).toBeEnabled();
  await box.fill(text);
  await box.press("Enter");
}

export async function operator(page: Page, email = OPERATOR): Promise<void> {
  await page.goto("/console/login");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("button", { name: "Log out" })).toBeVisible();
}

/** Settings through the API as the lead (test setup, not the scenario under test). */
export async function leadSettings(request: APIRequestContext, body: Record<string, unknown>): Promise<void> {
  const login = await request.post("/api/v1/auth/login", { data: { email: LEAD, password: PASSWORD } });
  expect(login.ok()).toBe(true);
  const { csrf_token } = (await login.json()) as { csrf_token: string };
  const put = await request.put("/api/v1/admin/settings", { data: { ...body, reason: "e2e setup" }, headers: { "X-CSRF-Token": csrf_token } });
  expect(put.ok(), await put.text()).toBe(true);
}
