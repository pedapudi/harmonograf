import { test, expect } from "../harness/fixtures.js";

// ===========================================================================
// Golden-path cross-component smoke (task #11)
// ===========================================================================
//
// What this exercises, end-to-end, with NO mocks:
//
//   [client lib]  --gRPC bidi-->  [server]  <--gRPC-Web--  [frontend in browser]
//
// The test boots all three via the harness, drives a synthetic client,
// then asserts the UI reflects what the client emitted. This is the one
// test that catches bugs no single-component suite can.
//
// Golden path script:
//
//   1. Boot server (sqlite store, ephemeral data dir) and Vite dev server.
//   2. Open the frontend at baseURL. App bar + nav rail visible.
//   3. Start a synthetic agent via the Python client library in a
//      sub-process (or via `tests/e2e/test_adk_hello.py` style direct
//      import). It creates a session `smoke-golden-path` and emits:
//        - INVOCATION (root)
//        - LLM_CALL (child, COMPLETED)
//        - TOOL_CALL (child, COMPLETED)
//        - INVOCATION closes COMPLETED
//   4. Assert the session picker shows `smoke-golden-path` under Live.
//   5. Click it. Assert the Gantt canvas renders one agent row and
//      three span blocks (spot-check via the DOM shadow of the block
//      count — not by pixel scraping).
//   6. Click the LLM_CALL block. Assert the inspector drawer opens
//      with the Overview tab selected and the span name visible.
//   7. Switch to Payload tab. Assert payload text renders (lazy load
//      fires GetPayload through the server).
//   8. Post an annotation via the Annotations tab. Assert it round-
//      trips and shows in the pins strip above the agent row.
//   9. Steering path: right-click the running LLM_CALL (from a second
//      synthetic that stays RUNNING), choose Steer, submit, assert the
//      client process receives the STEER control event on its bidi
//      stream (harness peeks at client log).
//  10. HITL path: synthetic emits AWAITING_HUMAN span. Assert the
//      approval snackbar appears. Click Approve. Assert the span
//      transitions to RUNNING and the snackbar dismisses.
//  11. Teardown: close the browser, kill server + synthetic, wipe data.
//
// Selector contract — the IDs listed below MUST exist on the frontend.
// If any are missing, the spec fails fast with a clear error rather
// than querying by fragile text or role. Coordinate with frontend-dev
// before changing any of these names.
//
//   data-testid="app-bar"                    shell/AppBar.tsx
//   data-testid="nav-rail"                   shell/NavRail.tsx
//   data-testid="session-picker"             SessionPicker (the trigger)
//   data-testid="session-picker-menu"        open menu surface
//   data-testid="session-picker-item"        each row; use getByText for title
//   data-testid="gantt-canvas"               Gantt root canvas wrapper
//   data-testid="gantt-agent-row"            one per agent; has data-agent-id
//   data-testid="gantt-span-block"           one per visible span; has data-span-id
//   data-testid="inspector-drawer"           Drawer root
//   data-testid="inspector-tab-overview"     tab buttons
//   data-testid="inspector-tab-payload"
//   data-testid="inspector-tab-annotations"
//   data-testid="inspector-span-name"        header text node
//   data-testid="payload-content"            lazy payload body
//   data-testid="annotation-compose-input"   inline text field
//   data-testid="annotation-submit"          enter-equivalent button
//   data-testid="pin-strip"                  above each agent row
//   data-testid="pin"                        single pin; has data-annotation-id
//   data-testid="span-context-menu"          right-click menu root
//   data-testid="span-context-menu-steer"    menu item
//   data-testid="steer-input"                inline steer field
//   data-testid="steer-submit"
//   data-testid="approval-snackbar"
//   data-testid="approval-approve"
//   data-testid="approval-reject"
//   data-testid="transport-bar"              bottom control bar
//
// Rationale for testid-over-role: the Gantt is Canvas2D — there are no
// native a11y nodes for spans. Wrapping each hittable element in a DOM
// proxy with a testid is how we drive it deterministically. The MD3
// chrome (drawer/menus/snackbars) has roles, but we still prefer
// testids for stability across @material/web versions.
// ===========================================================================

test.describe("golden path", () => {
  test.skip(
    true,
    "scaffold only — unblocks when tasks #4, #5, #6 land and selectors exist",
  );

  test("client emits -> server fans in -> frontend renders -> operator acts", async ({
    page,
    stack,
  }) => {
    await page.goto("/");

    await expect(page.getByTestId("app-bar")).toBeVisible();
    await expect(page.getByTestId("nav-rail")).toBeVisible();

    // Step 3: synthetic client emits. Left as TODO — will reuse the
    // ADK happy-path harness that e2e/test_adk_hello.py sets up.
    //
    // const synth = await startSyntheticAgent({
    //   serverPort: stack.serverPort,
    //   sessionId: "smoke-golden-path",
    // });

    // Step 4–6: session + gantt + inspector.
    await page.getByTestId("session-picker").click();
    await page.getByTestId("session-picker-item").getByText("smoke-golden-path").click();

    const rows = page.getByTestId("gantt-agent-row");
    await expect(rows).toHaveCount(1);

    const blocks = page.getByTestId("gantt-span-block");
    await expect(blocks).toHaveCount(3);

    await blocks.filter({ has: page.locator('[data-span-kind="LLM_CALL"]') }).first().click();
    await expect(page.getByTestId("inspector-drawer")).toBeVisible();
    await expect(page.getByTestId("inspector-span-name")).toContainText("llm");

    // Step 7: payload tab.
    await page.getByTestId("inspector-tab-payload").click();
    await expect(page.getByTestId("payload-content")).not.toBeEmpty();

    // Step 8: annotation round-trip.
    await page.getByTestId("inspector-tab-annotations").click();
    await page.getByTestId("annotation-compose-input").fill("smoke test note");
    await page.getByTestId("annotation-submit").click();
    await expect(page.getByTestId("pin").first()).toBeVisible();

    // Step 9 + 10: steering + HITL. Full wiring lands when the
    // harness spawns a synthetic that keeps a span RUNNING / enters
    // AWAITING_HUMAN on cue.
    void stack;
  });
});
