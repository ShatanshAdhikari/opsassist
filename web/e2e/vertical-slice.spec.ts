import { expect, test } from "@playwright/test";

const EMAIL = process.env.E2E_EMAIL ?? "";
const PASSWORD = process.env.E2E_PASSWORD ?? "";
const SHOTS = process.env.E2E_SCREENSHOT_DIR;

const RUNBOOK = `# Billing Service Runbook

Owner: payments-platform team.

## Restarting the service

Run systemctl restart billing-worker and confirm the invoice queue drains within five minutes.
`;

test("upload a document, ask a question, inspect the citation", async ({ page }) => {
  const sourceName = `E2E ${Date.now()}`;

  await page.goto("/");
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();

  await page.getByRole("button", { name: "Knowledge" }).click();
  await page.getByLabel("Name").fill(sourceName);
  await page.getByRole("button", { name: "Create source" }).click();

  await page.locator('input[type="file"]').setInputFiles({
    name: "billing-runbook.md",
    mimeType: "text/markdown",
    buffer: Buffer.from(RUNBOOK),
  });
  await expect(page.locator("td .badge-good", { hasText: "Indexed" })).toBeVisible({ timeout: 60_000 });
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/knowledge.png`, fullPage: true });

  await page.getByRole("button", { name: "Assistant" }).click();
  await page.getByLabel("Your question").fill("How do I restart the billing worker?");
  await page.getByRole("button", { name: "Ask", exact: true }).click();

  const chip = page.getByRole("button", { name: /^Citation S\d+: billing-runbook/ }).first();
  await expect(chip).toBeVisible({ timeout: 200_000 });
  await chip.click();

  const panel = page.getByRole("complementary");
  await expect(panel.getByText("billing-runbook")).toBeVisible();
  await expect(panel.getByText(/Restarting the service/)).toBeVisible();
  await expect(panel.locator(".excerpt").first()).toContainText("systemctl restart billing-worker");
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/answer.png`, fullPage: true });

  await page.setViewportSize({ width: 390, height: 800 });
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/mobile.png` });
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
  expect(overflow).toBe(false);
});
