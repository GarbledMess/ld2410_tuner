const assert = require("node:assert/strict");

async function assertAligned(page, selected = false) {
  await page.waitForFunction(() => {
    const card = panel.shadowRoot.querySelector('[data-device-id="a"]');
    const plot = card
      .querySelector('[data-role="selection-hit"]')
      .getBoundingClientRect();
    const bar = card.querySelector(".history-range").getBoundingClientRect();
    return (
      Math.abs(plot.left - bar.left) < 1 && Math.abs(plot.right - bar.right) < 1
    );
  });
  const geometry = await page.evaluate(() => {
    const card = panel.shadowRoot.querySelector('[data-device-id="a"]');
    const chart = panel._chartState.get("a");
    const rect = (selector) => {
      const r = card.querySelector(selector).getBoundingClientRect();
      return [r.left, r.right];
    };
    return {
      chart: { start: chart.data.start, end: chart.data.end },
      bar: panel._historyRangeBounds("a"),
      graphSelection: rect('[data-role="selection-rect"]'),
      barSelection: rect(".history-range-selection"),
    };
  });
  assert.deepEqual(
    geometry.bar,
    geometry.chart,
    "Graph and label bar must show the same time window",
  );
  if (selected)
    geometry.graphSelection.forEach((value, index) =>
      assert.ok(
        Math.abs(value - geometry.barSelection[index]) < 1,
        "The same selected instant must align within one pixel",
      ),
    );
}

module.exports = async function testHistoryGraph(page, screenshotDir) {
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
  await assertAligned(page);
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
  await assertAligned(page, true);
  await page.setViewportSize({ width: 390, height: 844 });
  await assertAligned(page, true);
  await page.setViewportSize({ width: 1400, height: 1000 });
  await assertAligned(page, true);
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
  await assertAligned(page, true);
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
  await assertAligned(page, true);
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
    fixture.devices.a.last_learning.review = {
      period_count: 1,
      periods: [
        {
          start: Date.parse("2026-09-23T10:00:12Z") / 1000,
          end: Date.parse("2026-09-23T10:00:30Z") / 1000,
          state: "not_present",
          samples: 3,
          short_burst: true,
          gates: { g3_still: 70 },
        },
      ],
    };
    return panel._load();
  });
  const review = card.locator(".evidence-review");
  await review.locator(":scope > summary").click();
  assert.match(await review.innerText(), /not proof of incorrect labels/);
  assert.match(await review.innerText(), /G3 still: 70/);
  await card
    .locator('.subsection[data-section="details"]')
    .screenshot({ path: `${screenshotDir}/evidence-review.png` });
  await page.evaluate(() => panel._load());
  assert.equal(await review.evaluate((node) => node.open), true);
  const writesBefore = await page.evaluate(
    () =>
      requests.filter((r) =>
        /\/(label_history|edit_history_label)$/.test(r.type),
      ).length,
  );
  await review.locator('[data-action="review-period"]').click();
  await page.waitForFunction(() => !panel._chartState.get("a").loading);
  await card
    .locator('.subsection[data-section="history"]')
    .screenshot({ path: `${screenshotDir}/aligned-history.png` });
  await page.setViewportSize({ width: 390, height: 844 });
  await assertAligned(page, true);
  await card
    .locator('.subsection[data-section="history"]')
    .screenshot({ path: `${screenshotDir}/aligned-history-mobile.png` });
  await page.setViewportSize({ width: 1400, height: 1000 });
  await assertAligned(page, true);
  assert.equal(
    await card.locator('[data-action="history-state"]').inputValue(),
    "not_present",
  );
  assert.equal(
    await card.locator('[data-action="history-start"]').inputValue(),
    "2026-09-23T11:00:12",
  );
  assert.equal(
    await page.evaluate(
      () =>
        requests.filter((r) =>
          /\/(label_history|edit_history_label)$/.test(r.type),
        ).length,
    ),
    writesBefore,
    "Reviewing evidence cannot silently change or exclude it",
  );
  await card.locator('[data-action="history-state"]').selectOption("unknown");
  page.once("dialog", (dialog) => dialog.accept());
  await card.locator('[data-action="history-apply"]').click();
  await page.waitForFunction(() => panel._activeActions === 0);
  assert.deepEqual(
    await page.evaluate(() => {
      const cs = panel._chartState.get("a");
      return [cs.gate, cs.kind];
    }),
    [3, "still"],
    "Review highlights the triggering gate",
  );
  const exclusion = await page.evaluate(() =>
    requests.findLast((r) => r.type.endsWith("/label_history")),
  );
  assert.equal(exclusion.state, "unknown");
  assert.equal(exclusion.end - exclusion.start, 18);
  await page.evaluate(() => {
    delete fixture.devices.a.last_learning.review;
    return panel._load();
  });
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
  assert.match(
    await card.locator(".learning-report").textContent(),
    /5 \/ 5000/,
  );
  assert.match(explanation, /Targets not met/);
  await page.evaluate(() => {
    fixture.devices.a.last_learning = window.beforeSuppression;
    delete fixture.devices.a.last_learning.feasibility;
    return panel._load();
  });
};
