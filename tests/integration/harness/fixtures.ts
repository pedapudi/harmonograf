import { test as base } from "@playwright/test";
import { bootStack, type BootedStack } from "./processes.js";

// Playwright fixture that boots the full stack once per worker. Specs
// import `test` from here instead of @playwright/test and get a
// ready-to-drive server + frontend plus the auto `stackUrl` fixture
// that points playwright at the ephemeral frontend port.

type WorkerFixtures = { stack: BootedStack };
type TestFixtures = { stackUrl: void };

export const test = base.extend<TestFixtures, WorkerFixtures>({
  stack: [
    async ({}, use) => {
      const stack = await bootStack();
      await use(stack);
      await stack.stop();
    },
    { scope: "worker" },
  ],
  stackUrl: [
    async ({ stack }, use, testInfo) => {
      testInfo.project.use.baseURL = stack.frontendUrl;
      await use();
    },
    { auto: true },
  ],
});

export { expect } from "@playwright/test";
