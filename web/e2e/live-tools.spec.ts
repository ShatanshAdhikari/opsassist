import { expect, test } from "@playwright/test";

// Needs a workspace with reviewed read-only GitHub tools and a local model. See docs/GITHUB_SETUP.md.
const EMAIL = process.env.E2E_EMAIL ?? "";
const PASSWORD = process.env.E2E_PASSWORD ?? "";
const REPO = process.env.E2E_GITHUB_REPO ?? "";
const SHOTS = process.env.E2E_SCREENSHOT_DIR;

test.skip(!REPO, "Set E2E_GITHUB_REPO to run against real GitHub");
test.setTimeout(900_000);

test("ask about a pull request, see the live call, inspect its citation", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();

  await page.getByRole("button", { name: "Assistant" }).click();
  await page.getByLabel("Your question").fill(`Get pull request number 1 in ${REPO}. What is its title and state?`);
  await page.getByRole("button", { name: "Ask", exact: true }).click();

  const calls = page.locator(".tool-calls");
  await expect(calls).toBeVisible({ timeout: 800_000 });
  await expect(calls.getByText("Returned data")).toBeVisible();
  await expect(calls.getByText(/github\.pull_get/)).toBeVisible();
  await expect(calls.getByText(/fetched/)).toBeVisible();
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/tool-answer.png`, fullPage: true });

  const chip = page.getByRole("button", { name: /^Citation S\d+: github\.pull_get/ }).first();
  await chip.click();
  const panel = page.getByRole("complementary");
  await expect(panel.getByText(/Live result from a connected system/)).toBeVisible();
  await expect(panel.locator(".excerpt")).toContainText('"number": 1');
  await expect(panel.getByRole("button", { name: "Open current source" })).toHaveCount(0);
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/tool-citation.png`, fullPage: true });

  await page.setViewportSize({ width: 390, height: 800 });
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/tool-mobile.png` });
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
  expect(overflow).toBe(false);
});
