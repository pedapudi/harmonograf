import { test as base } from "@playwright/test";
import { bootStack, type BootedStack } from "./processes.js";

// Playwright fixture that boots the full stack per-test-file (scope:
// "worker"). Specs import `test` from here instead of @playwright/test
// and get a ready-to-drive server + frontend.

export const test = base.extend<{ stack: BootedStack }, { stack: BootedStack }>({
  stack: [
    async ({}, use) => {
      const stack = await bootStack();
      await use(stack);
      await stack.stop();
    },
    { scope: "worker" },
  ],
});

export { expect } from "@playwright/test";
