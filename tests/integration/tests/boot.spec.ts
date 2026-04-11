import { test, expect } from "../harness/fixtures.js";

// Minimal smoke: prove the harness can boot the full stack and the
// frontend loads against the ephemeral server. This is the test we
// can run today; golden-path.spec.ts needs the full data-testid
// contract from task #14 before it un-skips.

test("full stack boots and frontend loads", async ({ page, stack }) => {
  await page.goto(stack.frontendUrl);
  // App shell renders its chrome unconditionally on load — these
  // testids are already landed as part of task #14 (shell group).
  await expect(page.getByTestId("app-bar")).toBeVisible();
  await expect(page.getByTestId("nav-rail")).toBeVisible();
});
