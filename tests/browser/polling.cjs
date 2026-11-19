const assert = require("node:assert/strict");

// Most of the suite drives refreshes itself. The panel's 3-second background poll
// replaces panel._data and DOM nodes at whatever moment it fires, so a test that
// edits panel state and then clicks can lose its edit to a poll landing in between.
// Attaching the panel (connectedCallback) or setting hass starts the poll again.

async function pausePolling(page) {
  await page.evaluate(async () => {
    clearInterval(panel._pollTimer);
    panel._pollTimer = null;
    await panel._loadPromise; // A poll already in flight must land first.
  });
}

async function assertPollingPaused(page, step) {
  assert.equal(
    await page.evaluate(() => Boolean(panel._pollTimer)),
    false,
    `${step} left the panel's background poll running; call pausePolling after attaching the panel`,
  );
}

module.exports = { pausePolling, assertPollingPaused };
