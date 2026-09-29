const assert = require("node:assert/strict");
const path = require("node:path");

module.exports = async function testComparison(page, screenshotDir) {
  await page.evaluate(async () => {
    window.beforeComparison = structuredClone(fixture.devices.a);
    window.beforeComparisonSelection = panel._learningSelection.get("a");
    window.comparisonWS = panel._hass.callWS;
    fixture.devices.a.learning_results = Object.fromEntries(
      ["user", "previous", "current", "automatic"].map((slot) => [
        slot,
        { ...structuredClone(fixture.devices.a.last_learning), id: slot },
      ]),
    );
    const metric = {
      present_samples: 100,
      not_present_samples: 100,
      false_negatives: 2,
      false_positives: 3,
    };
    window.comparisonReport = {
      state: "ready",
      evaluated_at: Date.now() / 1000,
      best_slots: ["automatic"],
      timing: { mode: "disabled" },
      patterns: Object.fromEntries(
        ["live", "user", "previous", "current", "automatic"].map(
          (slot, index) => [
            slot,
            {
              result_id: slot === "live" ? null : slot,
              score: 95 + index,
              applicable: slot !== "live",
              basis: "human",
              presence_recall: 0.998,
              false_positive_percent: 0.25,
              target_met: false,
              human: metric,
            },
          ],
        ),
      ),
    };
    fixture.devices.a.comparison = { state: "pending" };
    panel._hass.callWS = async (message) => {
      if (message.type.endsWith("/compare_results")) {
        requests.push(message);
        if (window.comparisonFail)
          throw new Error("Comparison connection failed");
        await new Promise((resolve) => {
          window.finishComparison = resolve;
        });
        fixture.devices.a.comparison = structuredClone(comparisonReport);
        return comparisonReport;
      }
      return comparisonWS(message);
    };
    panel._collapsed.add("a");
    await panel._load(true);
  });
  const card = page.locator('[data-device-id="a"]');
  const header = card.locator('[data-action="review-comparison"]');
  assert.match(await header.innerText(), /Current —.*Best —/);
  assert.equal(await header.isVisible(), true);
  await header.click();
  assert.match(
    await card.locator(".pattern-comparison").innerText(),
    /Comparing against shared recordings/,
  );
  await page.evaluate(() => finishComparison());
  await page.waitForFunction(
    () => panel._data.devices.a.comparison.state === "ready",
  );
  assert.match(await header.innerText(), /Current 95.00\/100.*Best 99.00\/100/);
  assert.equal(await card.locator("[data-pattern]").count(), 5);
  assert.match(
    await card.locator('[data-pattern="current"]').innerText(),
    /98.00\/100/,
  );
  assert.match(
    await card.locator('[data-pattern="automatic"]').innerText(),
    /Best/,
  );
  const beforeWrites = await page.evaluate(
    () => requests.filter((m) => m.type.endsWith("/apply")).length,
  );
  await card.locator('[data-slot="automatic"]').click();
  assert.equal(
    await card.locator('[data-action="learning-result"]').inputValue(),
    "automatic",
  );
  assert.equal(
    await page.evaluate(
      () => requests.filter((m) => m.type.endsWith("/apply")).length,
    ),
    beforeWrites,
  );
  const samples = card.locator('[data-pattern="live"] details');
  await samples.locator("summary").click();
  assert.match(
    await samples.innerText(),
    /98 hits, 2 misses, 3 false triggers, 97 correct empty/,
  );
  await card.locator('[data-detail="comparison-score"] summary').click();
  assert.match(
    await card.locator(".pattern-comparison").innerText(),
    /not a probability or a guarantee/,
  );
  await page.setViewportSize({ width: 390, height: 844 });
  for (const slot of ["live", "user", "previous", "current", "automatic"])
    assert.equal(
      await card.locator(`[data-pattern="${slot}"]`).isVisible(),
      true,
      `${slot} remains visible on mobile`,
    );
  await card.locator('[data-slot="previous"]').click();
  assert.equal(
    await card.locator('[data-action="learning-result"]').inputValue(),
    "previous",
  );

  await card
    .locator(".pattern-comparison")
    .screenshot({ path: path.join(screenshotDir, "comparison-mobile.png") });
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
    true,
  );
  const cellsFit = await card.locator(".comparison-table").evaluate((table) => {
    const bounds = table.parentElement.getBoundingClientRect();
    return [...table.querySelectorAll("td")].every((cell) => {
      const box = cell.getBoundingClientRect();
      return box.left >= bounds.left && box.right <= bounds.right + 1;
    });
  });
  assert.equal(
    cellsFit,
    true,
    "scores and review controls fit inside the mobile card",
  );
  const bounds = await card.locator(".pattern-comparison").boundingBox();
  assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= 390);
  await page.setViewportSize({ width: 1400, height: 1000 });
  await card
    .locator(".pattern-comparison")
    .screenshot({ path: path.join(screenshotDir, "comparison-desktop.png") });
  await page.evaluate(() => {
    window.comparisonFail = true;
  });
  await card.locator('[data-action="refresh-comparison"]').click();
  await page.waitForFunction(() => panel._comparisonErrors.has("a"));
  await page.waitForFunction(() => !panel._loadPromise);
  assert.match(
    await card.locator(".pattern-comparison").innerText(),
    /Comparison connection failed/,
  );
  assert.equal(
    await card.locator('[data-action="refresh-comparison"]').isEnabled(),
    true,
  );
  await page.evaluate(async () => {
    fixture.devices.a = beforeComparison;
    panel._hass.callWS = comparisonWS;
    panel._comparisonErrors.clear();
    if (beforeComparisonSelection)
      panel._learningSelection.set("a", beforeComparisonSelection);
    else panel._learningSelection.delete("a");
    await panel._load(true);
  });
};
