import { expect, test } from "@playwright/test";
import { customer, LEAD, leadSettings, noAxeViolations, operator, SEPA, send } from "./helpers";

// UI §6 scenarios, in order on one database, with fake models.

test("public pages pass the accessibility check", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await noAxeViolations(page);
  await page.goto("/chat");
  await noAxeViolations(page);
  await page.goto("/console/login");
  await noAxeViolations(page);
  await page.goto("/help/withdrawal-times");
  await expect(page.getByRole("heading", { name: "How long does a withdrawal take?" })).toBeVisible();
  await noAxeViolations(page);
});

test("a customer asks a routine question and gets a cited answer (auto-reply on)", async ({ page, request }) => {
  await leadSettings(request, { auto_reply_enabled: true, confidence_threshold: 0.01, intent_allowlist: ["withdrawal_issue"] });
  await customer(page);
  await noAxeViolations(page);
  await send(page, SEPA);
  const sources = page.getByText("Sources");
  await expect(sources).toBeVisible({ timeout: 45_000 });
  const link = page.getByRole("link", { name: "How long does a withdrawal take?" }).first();
  await expect(link).toHaveAttribute("href", /^\/help\/withdrawal-times#/);
  await noAxeViolations(page);
});

test("a lead switches auto-reply off and the next ticket goes to approval", async ({ page, browser }) => {
  await operator(page, LEAD);
  await page.goto("/console/settings");
  await page.getByRole("switch", { name: /Switch auto-reply off/ }).click();
  await page.getByLabel(/Reason/).fill("e2e: back to review");
  await page.getByRole("button", { name: "Confirm change" }).click();
  await expect(page.getByRole("switch", { name: /Switch auto-reply on/ })).toBeVisible();
  await noAxeViolations(page);

  const chat = await (await browser.newContext()).newPage();
  await customer(chat, "Petra");
  await send(chat, SEPA);
  await expect(chat.getByText("A support specialist is checking the answer")).toBeVisible({ timeout: 45_000 });
});

test("a customer pastes a card number and sees it masked", async ({ page }) => {
  await customer(page, "Chloe");
  await send(page, "My card 4111 1111 1111 1111 was charged twice, please help");
  await expect(page.getByText("We hid a card number for your safety.")).toBeVisible({ timeout: 45_000 });
  await expect(page.getByText("4111 1111 1111 1111")).toHaveCount(0);
});

test("text that looks like markup or links stays text", async ({ page }) => {
  await customer(page, "Jonas");
  const text = "<img src=x onerror=alert(1)> <script>alert(2)</script> [x](javascript:alert(3)) https://evil.example";
  await send(page, text);
  const messages = page.getByRole("list", { name: "Messages" });
  await expect(messages.getByText(text)).toBeVisible({ timeout: 45_000 });
  await expect(messages.locator("img, script, a")).toHaveCount(0);
});

test("fraud report: handoff notice, operator replies, customer sees the reply", async ({ page, browser }) => {
  await customer(page, "Priya");
  await send(page, "Someone took 900 EUR from my account without my permission. This is fraud!");
  await expect(page.getByRole("note")).toBeVisible({ timeout: 45_000 });

  const console = await (await browser.newContext()).newPage();
  await operator(console);
  await console.getByRole("tab", { name: /Handed off/ }).click();
  await console.getByRole("link", { name: "Priya Raghunathan" }).first().click();
  await console.getByLabel("Your reply").fill("We have frozen your card and opened a fraud case.");
  await console.getByRole("button", { name: "Send reply" }).click();

  await expect(page.getByText("We have frozen your card and opened a fraud case.")).toBeVisible({ timeout: 15_000 });
});

async function ticketAwaitingApproval(browser: import("@playwright/test").Browser, who: string) {
  const chat = await (await browser.newContext()).newPage();
  await customer(chat, who);
  await send(chat, SEPA);
  await expect(chat.getByText("A support specialist is checking the answer")).toBeVisible({ timeout: 45_000 });
  return chat;
}

test("an operator approves a draft and the customer sees it", async ({ page, browser }) => {
  const chat = await ticketAwaitingApproval(browser, "Thornbury");
  await operator(page);
  await noAxeViolations(page);
  await page.getByRole("link", { name: "Thornbury Analytics Ltd" }).first().click();
  await expect(page.getByRole("heading", { name: "AI draft" })).toBeVisible();
  await noAxeViolations(page);
  await page.getByRole("button", { name: "Approve and send" }).click();
  await expect(chat.getByText(/reviewed by Avery/)).toBeVisible({ timeout: 15_000 });
});

test("an operator edits and sends", async ({ page, browser }) => {
  const chat = await ticketAwaitingApproval(browser, "Lucia");
  await operator(page);
  await page.getByRole("link", { name: "Lucia Ferrandis" }).first().click();
  const editor = page.getByLabel(/Answer to the customer/);
  await editor.fill("Your SEPA withdrawal usually arrives within one business day. (checked by our team)");
  await expect(page.getByRole("button", { name: "Approve and send" })).toBeDisabled();
  await page.getByRole("button", { name: "Send edited" }).click();
  await expect(chat.getByText("(checked by our team)")).toBeVisible({ timeout: 15_000 });
  await expect(chat.getByText(/Avery · Support team/)).toBeVisible();
});

test("a stale draft shows the conflict banner", async ({ page, browser }) => {
  const chat = await ticketAwaitingApproval(browser, "Chloe");
  await operator(page);
  await page.getByRole("link", { name: "Chloe Marchetti" }).first().click();
  await expect(page.getByRole("heading", { name: "AI draft" })).toBeVisible();
  // The customer writes again while the operator reads the draft.
  await send(chat, "Any news?");
  await expect(chat.getByLabel("Your message")).toBeEnabled({ timeout: 45_000 });
  await page.getByRole("button", { name: "Approve and send" }).click();
  await expect(page.getByText("The customer wrote again.")).toBeVisible();
});

test("an operator cannot open the settings", async ({ page }) => {
  await operator(page);
  await expect(page.getByRole("link", { name: "Settings" })).toHaveCount(0);
  await page.goto("/console/settings");
  await expect(page.getByText("Your role (operator) cannot open this page.")).toBeVisible();
  const api = await page.request.get("/api/v1/admin/settings");
  expect(api.status()).toBe(403);
});

test("lead pages pass the accessibility check", async ({ page }) => {
  await operator(page, LEAD);
  for (const path of ["/console/dashboard", "/console/settings", "/console/audit", "/console/kb"]) {
    await page.goto(path);
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    await noAxeViolations(page);
  }
});
