const assert = require("node:assert/strict");

async function touchDrag(session, start, end) {
  await session.send("Input.dispatchTouchEvent", {
    type: "touchStart",
    touchPoints: [{ x: start.x, y: start.y, id: 1 }],
  });
  for (let step = 1; step <= 8; step++) {
    await session.send("Input.dispatchTouchEvent", {
      type: "touchMove",
      touchPoints: [
        {
          x: start.x + ((end.x - start.x) * step) / 8,
          y: start.y + ((end.y - start.y) * step) / 8,
          id: 1,
        },
      ],
    });
  }
  await session.send("Input.dispatchTouchEvent", {
    type: "touchEnd",
    touchPoints: [],
  });
}

module.exports = async function testTouchSelection(page) {
  await page.setViewportSize({ width: 390, height: 844 });
  const session = await page.context().newCDPSession(page);
  await session.send("Emulation.setTouchEmulationEnabled", { enabled: true });
  await page.evaluate(() => {
    const state = panel._chartState.get("a");
    state.selection = null;
    panel._renderChartCanvas("a", true);
    window.touchEvents = [];
    for (const type of ["pointerdown", "pointerup", "pointercancel"])
      window.addEventListener(type, (event) => touchEvents.push(event.type));
  });
  const hit = page.locator('[data-device-id="a"] [data-role="selection-hit"]');
  await hit.scrollIntoViewIfNeeded();
  const box = await hit.boundingBox();
  await touchDrag(
    session,
    { x: box.x + box.width * 0.2, y: box.y + box.height / 2 },
    { x: box.x + box.width * 0.7, y: box.y + box.height / 2 + 15 },
  );
  const result = await page.evaluate(() => ({
    selection: panel._chartState.get("a").selection,
    dragging: panel._dragging,
    events: touchEvents,
  }));
  assert.ok(
    result.selection,
    `Touch must select a range; events: ${result.events}`,
  );
  assert.equal(result.dragging, false);
  assert.ok(
    !result.events.includes("pointercancel"),
    "The browser must not steal the gesture for scrolling",
  );
  const handle = page.locator(
    '[data-device-id="a"] [data-role="handle-start"] .handle-hit',
  );
  const handleBox = await handle.boundingBox();
  assert.ok(
    handleBox.width >= 43,
    "Touch handles need a finger-sized hit area",
  );
  const from = {
    x: handleBox.x + handleBox.width / 2,
    y: handleBox.y + handleBox.height / 2,
  };
  await touchDrag(session, from, { x: from.x + 20, y: from.y });
  const moved = await page.evaluate(() => panel._chartState.get("a").selection);
  assert.ok(
    moved.start > result.selection.start,
    "Touch can resize the start boundary",
  );
  assert.equal(moved.end, result.selection.end);
  const current = await handle.boundingBox();
  await session.send("Input.dispatchTouchEvent", {
    type: "touchStart",
    touchPoints: [
      {
        x: current.x + current.width / 2,
        y: current.y + current.height / 2,
        id: 1,
      },
    ],
  });
  await session.send("Input.dispatchTouchEvent", {
    type: "touchMove",
    touchPoints: [
      {
        x: current.x + current.width / 2 + 15,
        y: current.y + current.height / 2,
        id: 1,
      },
    ],
  });
  await page.evaluate(() =>
    window.dispatchEvent(new PointerEvent("pointerup", { pointerId: 99 })),
  );
  assert.equal(
    await page.evaluate(() => panel._dragging),
    true,
    "Another finger cannot finish the active drag",
  );
  await session.send("Input.dispatchTouchEvent", {
    type: "touchCancel",
    touchPoints: [],
  });
  assert.deepEqual(
    await page.evaluate(() => panel._chartState.get("a").selection),
    moved,
    "Canceled resize restores the previous range",
  );
  assert.equal(await page.evaluate(() => panel._dragging), false);
  await session.send("Emulation.setTouchEmulationEnabled", { enabled: false });
  await session.detach();
};
