const assert = require("node:assert/strict");

module.exports = async function testTimeline(page) {
  await page.setViewportSize({ width: 390, height: 844 });
  const card = page.locator('[data-device-id="a"]');
  await card.locator('[data-action="history-new"]').click();
  const bar = card.locator(".history-range");
  await bar.scrollIntoViewIfNeeded();
  const box = await bar.boundingBox();
  const session = await page.context().newCDPSession(page);
  await session.send("Emulation.setTouchEmulationEnabled", { enabled: true });
  const touch = (type, x) =>
    session.send("Input.dispatchTouchEvent", {
      type,
      touchPoints:
        type === "touchEnd" || type === "touchCancel"
          ? []
          : [{ x, y: box.y + box.height / 2, id: 1 }],
    });
  const from = card.locator('[data-action="history-start"]');
  const to = card.locator('[data-action="history-end"]');
  await touch("touchStart", box.x + box.width * 0.2);
  await touch("touchMove", box.x + box.width * 0.7);
  assert.equal(
    await card.locator(".history-range-selection").isVisible(),
    true,
  );
  await touch("touchEnd");
  const selected = {
    start: await from.inputValue(),
    end: await to.inputValue(),
  };
  assert.match(selected.start, /2026-09-23T04:/);
  assert.match(selected.end, /2026-09-23T16:/);
  assert.equal(
    await page.evaluate(() => panel._historyStateFor("a").edit),
    null,
  );
  assert.equal(await page.evaluate(() => panel._dragging), false);
  await page.evaluate(() => panel._load());
  assert.equal(await from.inputValue(), selected.start);
  assert.equal(
    await card.locator(".history-range-selection").isVisible(),
    true,
  );
  const handle = card.locator('[data-boundary="start"]');
  const hb = await handle.boundingBox();
  assert.ok(hb.width >= 44 && hb.height >= 44);
  await touch("touchStart", hb.x + hb.width / 2);
  await touch("touchMove", hb.x + hb.width / 2 + 20);
  await touch("touchEnd");
  assert.ok((await from.inputValue()) > selected.start);
  assert.equal(await to.inputValue(), selected.end);
  const resized = await from.inputValue();
  const hb2 = await handle.boundingBox();
  await touch("touchStart", hb2.x + hb2.width / 2);
  await touch("touchMove", hb2.x + hb2.width / 2 + 20);
  await touch("touchCancel");
  assert.equal(await from.inputValue(), resized, "cancel restores the range");
  assert.equal(await page.evaluate(() => panel._dragging), false);
  await session.send("Emulation.setTouchEmulationEnabled", { enabled: false });
  await session.detach();
  await handle.focus();
  const before = Number(await handle.getAttribute("aria-valuenow"));
  await handle.press("ArrowRight");
  assert.equal(Number(await handle.getAttribute("aria-valuenow")), before + 60);
  await from.fill("2026-09-23T08:00");
  assert.match(await handle.getAttribute("aria-valuetext"), /08:00/);
  await from.blur();
  // Saving the dragged range still requires the existing explicit confirmation.
  await card.locator('[data-action="history-state"]').selectOption("present");
  page.once("dialog", (dialog) => dialog.accept());
  await card.locator('[data-action="history-apply"]').click();
  await page.waitForFunction(() => panel._activeActions === 0);
  const request = await page.evaluate(() =>
    requests.findLast((r) => r.type.endsWith("/label_history")),
  );
  assert.equal(request.start, Date.parse("2026-09-23T08:00:00+01:00") / 1000);
  assert.equal(request.state, "present");
  // Crossing midnight is shown clipped, without altering the saved period.
  await card.locator('[data-action="history-edit"]').first().click();
  assert.equal(
    await card.locator('[data-boundary="start"]').isVisible(),
    false,
  );
  assert.equal(await card.locator('[data-boundary="end"]').isVisible(), true);
  await page.evaluate(() => {
    const card = panel.shadowRoot.querySelector('[data-device-id="a"]');
    const bounds = panel._historyDayBounds(panel._historyStateFor("a").day);
    panel._setHistoryRange(
      card,
      "a",
      { start: bounds.start, end: bounds.start + 30 },
      true,
    );
  });
  await handle.focus();
  await handle.press("ArrowRight");
  const shortRange = await page.evaluate(() =>
    panel._historyRangeValue(
      panel.shadowRoot.querySelector('[data-device-id="a"]'),
      "a",
    ),
  );
  assert.equal(
    shortRange.end - shortRange.start,
    1,
    "Resizing a short label cannot cross its other boundary",
  );
  const dst = await page.evaluate(async () => {
    const card = panel.shadowRoot.querySelector('[data-device-id="a"]');
    panel._changeHistoryDay(card, "a", "2026-10-25");
    const range = {
      start: Date.parse("2026-10-25T01:30:00Z") / 1000,
      end: Date.parse("2026-10-25T02:00:00Z") / 1000,
    };
    panel._setHistoryRange(card, "a", range, true);
    await panel._load();
    return {
      expected: range,
      actual: panel._historyRangeValue(
        panel.shadowRoot.querySelector('[data-device-id="a"]'),
        "a",
      ),
    };
  });
  assert.deepEqual(
    dst.actual,
    dst.expected,
    "Timeline retains the later occurrence of the repeated DST hour",
  );
  await card.locator('[data-action="history-day"]').fill("2026-09-23");
  await card.locator('[data-action="history-day"]').blur();
  await card.locator('[data-action="history-edit"]').first().click();
};
