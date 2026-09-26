const assert = require("node:assert/strict");

module.exports = async function testHistoryGraph(page) {
  await page.setViewportSize({ width: 1400, height: 1000 });
  const card = page.locator('[data-device-id="a"]');
  await page.evaluate(() => {
    const card = panel.shadowRoot.querySelector('[data-device-id="a"]');
    panel._setSectionCollapsed("a", "history", false);
    card
      .querySelector('.subsection[data-section="history"]')
      .classList.remove("collapsed");
    panel._changeHistoryDay(card, "a", "2026-09-23");
  });
  await page.waitForFunction(() => !panel._chartState.get("a").loading);
  const end = Date.parse("2026-09-24T00:00:00+01:00") / 1000;
  assert.equal(
    await page.evaluate(() => panel._chartState.get("a").data.end),
    end,
  );
  assert.equal(
    await card
      .locator('.history-graph .subsection[data-section="chart"]')
      .count(),
    1,
  );
  assert.equal(
    await card.locator(".chart-canvas").count(),
    1,
    "Only one graph and request lifecycle per device",
  );
  await card.locator('[data-action="chart-refresh"]').click();
  await page.waitForFunction(() => !panel._chartState.get("a").loading);
  assert.equal(
    await page.evaluate(() => panel._chartState.get("a").data.end),
    end,
    "Refresh cannot jump a historical editor to the present",
  );
  await card.locator('[data-action="history-edit"]').first().click();
  await page.waitForFunction(() => !panel._chartState.get("a").loading);
  const state = await page.evaluate(() => {
    const cs = panel._chartState.get("a");
    return { start: cs.data.start, end: cs.data.end, selection: cs.selection };
  });
  assert.ok(
    state.start < state.selection.start && state.end > state.selection.end,
  );
  assert.equal(
    await card.locator('[data-action="chart-range"]').inputValue(),
    String((state.end - state.start) / 3600),
  );
  const hit = card.locator('[data-role="selection-hit"]');
  await hit.scrollIntoViewIfNeeded();
  const box = await hit.boundingBox();
  await page.mouse.move(box.x + box.width * 0.3, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * 0.7, box.y + box.height / 2);
  await page.mouse.up();
  const synced = await page.evaluate(() => {
    const card = panel.shadowRoot.querySelector('[data-device-id="a"]');
    return {
      chart: panel._chartState.get("a").selection,
      fields: panel._historyRangeValue(card, "a"),
    };
  });
  assert.deepEqual(
    synced.fields,
    synced.chart,
    "Dragging the graph fills the existing editor",
  );
  await page.evaluate(() => panel._load());
  assert.equal(await card.locator(".history-graph .chart-canvas").count(), 1);
  await card
    .locator('[data-action="section-toggle"][data-section="history"]')
    .click();
  assert.equal(
    await card.locator(".chart-home .chart-canvas").isVisible(),
    true,
  );
  assert.equal(await card.locator(".chart-canvas").count(), 1);
  await card
    .locator('[data-action="section-toggle"][data-section="history"]')
    .click();
  await page.waitForFunction(() => !panel._chartState.get("a").loading);
  assert.equal(
    await card.locator(".history-graph .chart-canvas").isVisible(),
    true,
  );
  await page.evaluate(() => {
    fixture.devices.a.last_learning.feasibility = {
      status: "conflict",
      windows: [
        {
          conflict: true,
          scope: "all_human",
          periods: [
            {
              start: Date.parse("2026-09-23T10:00:00Z") / 1000,
              end: Date.parse("2026-09-23T10:01:00Z") / 1000,
              samples: 10,
            },
          ],
        },
      ],
    };
    return panel._load();
  });
  await card.locator('[data-action="review-period"]').click();
  await page.waitForFunction(() => !panel._chartState.get("a").loading);
  assert.equal(
    await card.locator('[data-action="history-start"]').inputValue(),
    "2026-09-23T11:00",
  );
  assert.equal(
    await card.locator('[data-action="history-end"]').inputValue(),
    "2026-09-23T11:01",
  );
  assert.equal(
    await page.evaluate(() => panel._historyStateFor("a").edit),
    null,
  );
  await page.evaluate(() => {
    window.beforeSuppression = structuredClone(fixture.devices.a.last_learning);
    const learning = fixture.devices.a.last_learning;
    learning.status = "unsafe";
    learning.training.present_samples = 5000;
    learning.training.false_negatives = 5;
    learning.proposals.g0_move.threshold = 100;
    const cs = panel._chartState.get("a");
    cs.kind = "move";
    cs.gate = 0;
    return panel._load();
  });
  const explanation = await card.locator(".learn-status").innerText();
  assert.match(explanation, /threshold 100 disables this gate/);
  assert.match(explanation, /combined gates detect 4995 \/ 5000/);
  assert.match(explanation, /5 are missed by every gate/);
  assert.match(
    explanation,
    /Combined device targets not met; manual Apply is available/,
  );
  await page.evaluate(() => {
    fixture.devices.a.last_learning = window.beforeSuppression;
    delete fixture.devices.a.last_learning.feasibility;
    return panel._load();
  });
};
