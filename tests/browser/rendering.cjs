const assert = require("node:assert/strict");
const path = require("node:path");

module.exports = async function testStableRendering(page, screenshotDir) {
  const settings = page.locator(".global-settings-card");
  assert.equal(
    await settings.evaluate((node) => node.open),
    false,
    "global settings start closed",
  );
  assert.equal(await page.locator("#learning-schedule").isVisible(), false);
  await settings.locator(":scope > summary").focus();
  await page.keyboard.press("Enter");
  assert.equal(await settings.evaluate((node) => node.open), true);
  await page.evaluate(async () => {
    window.renderingOriginal = structuredClone(fixture);
    window.stableNodes = {
      card: panel.shadowRoot.querySelector('[data-device-id="a"]'),
      other: panel.shadowRoot.querySelector('[data-device-id="b"]'),
      settings: panel.shadowRoot.querySelector(".global-settings-card"),
      input: panel.shadowRoot.querySelector('[data-action="nightly-time"]'),
      comparison: panel.shadowRoot.querySelector(".pattern-comparison"),
      chart: panel.shadowRoot.querySelector('[data-chart-canvas="a"] svg'),
    };
    window.renderingDetailsOpen =
      stableNodes.comparison.querySelector("details").open;
    stableNodes.comparison.querySelector("details").open = true;
    fixture.devices.a.learning_job = {
      status: "running",
      stage: "fitting",
      started_at: Date.now() / 1000 - 10,
      sources: ["user"],
    };
    await panel._load(true);
    stableNodes.progress = stableNodes.card.querySelector("progress");
    window.stableChanges = [];
    window.renderObserver = new MutationObserver((changes) =>
      stableChanges.push(...changes),
    );
    renderObserver.observe(stableNodes.comparison, {
      childList: true,
      subtree: true,
      characterData: true,
    });
    for (let i = 0; i < 3; i++) {
      fixture.devices.a.auto_learning = {
        last: { state: "present", confidence: 0.8 + i / 100, top_gates: [] },
        observations: i,
      };
      fixture.devices.a.learning_job.started_at -= 3;
      await panel._load(true);
    }
  });
  const result = await page.evaluate(() => ({
    card:
      stableNodes.card ===
      panel.shadowRoot.querySelector('[data-device-id="a"]'),
    other:
      stableNodes.other ===
      panel.shadowRoot.querySelector('[data-device-id="b"]'),
    settings:
      stableNodes.settings ===
      panel.shadowRoot.querySelector(".global-settings-card"),
    input:
      stableNodes.input ===
      panel.shadowRoot.querySelector('[data-action="nightly-time"]'),
    comparison:
      stableNodes.comparison ===
      panel.shadowRoot.querySelector(".pattern-comparison"),
    chart:
      stableNodes.chart ===
      panel.shadowRoot.querySelector('[data-chart-canvas="a"] svg'),
    progress:
      stableNodes.progress === stableNodes.card.querySelector("progress"),
    open: stableNodes.comparison.querySelector("details").open,
    settingsOpen: stableNodes.settings.open,
    changes: stableChanges.length,
    header: stableNodes.card.querySelector(".name").textContent,
  }));
  for (const key of [
    "card",
    "other",
    "settings",
    "input",
    "comparison",
    "chart",
    "progress",
    "open",
    "settingsOpen",
  ])
    assert.equal(result[key], true, `${key} is preserved across polls`);
  assert.equal(
    result.changes,
    0,
    "unrelated telemetry does not rewrite the comparison",
  );
  assert.match(result.header, /82%/);
  await page.evaluate(async () => {
    renderObserver.disconnect();
    stableNodes.comparison.querySelector("details").open = renderingDetailsOpen;
    fixture = renderingOriginal;
    await panel._load(true);
  });
  await settings.locator(":scope > summary").click();
  await page.evaluate(() => panel._load(true));
  assert.equal(await settings.evaluate((node) => node.open), false);
  await page.setViewportSize({ width: 390, height: 844 });
  await settings.screenshot({
    path: path.join(screenshotDir, "global-settings-collapsed-mobile.png"),
  });
  await settings.locator(":scope > summary").click();
  await settings.screenshot({
    path: path.join(screenshotDir, "global-settings-expanded-mobile.png"),
  });
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
    true,
  );
  await settings.locator(":scope > summary").click();
  await page.setViewportSize({ width: 1400, height: 1000 });
};
