const assert = require("node:assert/strict");

// The graph's selection toolbar labels like every other editor: From, To and a Status
// dropdown, then "Label time range". Choosing a status alone sends nothing.
module.exports = async function testSelectionStatus(page) {
  // With the history editor collapsed the graph sits under Gate visualization, where the
  // toolbar is shown (inside the history editor the editor itself labels).
  await page.evaluate(async () => {
    window.selectionHistoryWas = panel._sectionCollapsed("a", "history");
    panel._setSectionCollapsed("a", "history", true);
    await panel._load(true);
    const cs = panel._chartState.get("a");
    cs.historyLinked = false;
    cs.selection = { start: cs.data.start + 600, end: cs.data.start + 1800 };
    cs.panelOpen = true;
    panel._renderChartCanvas("a", true);
  });
  const toolbar = page.locator('[data-device-id="a"] .selection-panel');
  assert.equal(await toolbar.count(), 1);
  assert.equal(
    await toolbar.locator('[data-action="selection-label"]').count(),
    0,
    "no button labels on its own",
  );
  assert.deepEqual(
    await toolbar
      .locator('[data-role="selection-state"] option')
      .allInnerTexts(),
    ["Present", "Empty", "Unknown"],
  );
  const apply = toolbar.locator('[data-action="selection-apply"]');
  assert.equal(await apply.innerText(), "Label time range");
  const labelled = () =>
    page.evaluate(() =>
      requests.filter((m) => m.type.endsWith("/label_history")),
    );
  const before = (await labelled()).length;
  await toolbar
    .locator('[data-role="selection-state"]')
    .selectOption("not_present");
  assert.equal(
    (await labelled()).length,
    before,
    "choosing a status sends nothing",
  );
  await apply.click();
  await page.waitForFunction(
    (count) =>
      requests.filter((m) => m.type.endsWith("/label_history")).length > count,
    before,
  );
  const sent = (await labelled()).at(-1);
  assert.equal(sent.device_id, "a");
  assert.equal(sent.state, "not_present");
  await page.evaluate(async () => {
    const cs = panel._chartState.get("a");
    cs.selection = null;
    cs.panelOpen = false;
    panel._setSectionCollapsed("a", "history", selectionHistoryWas);
    await panel._load(true);
  });
};
