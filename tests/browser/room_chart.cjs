const assert = require("node:assert/strict");

// The room chart reads like the radar chart: every gate of the chosen kind,
// and each radar's own threshold for the highlighted gate.
module.exports = async function testRoomChart(page) {
  const gid = "area:ux";
  const timeline = page.locator(`[data-room-timeline="${gid}"]`);
  const graph = timeline.locator("[data-room-graph]");
  const setup = await page.evaluate(async (gid) => {
    panel._data.devices.a.current_thresholds = { g3_still: 40, g3_move: 30 };
    panel._data.devices.b.current_thresholds = { g3_still: 65, g3_move: 35 };
    const state = panel._roomGraphState(gid);
    state.kind = "still";
    state.gate = 3;
    const start = requests.length;
    await panel._loadRoomGraph(gid, panel._roomsData().groups[gid], true);
    const calls = requests
      .slice(start)
      .filter((r) => r.type.endsWith("history_series_multi"));
    return {
      calls: calls.map((r) => ({
        id: r.device_id,
        summary: !!r.summary,
        keys: r.keys,
      })),
      kinds: [
        ...panel
          ._roomGraphSection(gid)
          .querySelectorAll("[data-room-graph-kind] option"),
      ].map((option) => option.value),
    };
  }, gid);
  const stillKeys = Array.from({ length: 9 }, (_, g) => `g${g}_still`);
  assert.deepEqual(
    setup.calls,
    [
      { id: "a", summary: false, keys: stillKeys },
      { id: "b", summary: false, keys: stillKeys },
    ],
    "each radar's gates are requested, not a merged strongest signal",
  );
  assert.deepEqual(setup.kinds, ["move", "still"]);
  await graph.locator("svg").waitFor();
  for (const id of ["a", "b"])
    assert.equal(
      await graph.locator(`[data-room-signal="${id}"] .gate-line`).count(),
      8,
      "every other gate of the radar is drawn",
    );
  const thresholds = await graph
    .locator(".threshold-line.current + text")
    .allTextContents();
  assert.deepEqual(
    thresholds,
    ["Room a G3 40", "Room b G3 65"],
    "each radar shows its own threshold for the highlighted gate",
  );

  const before = await page.evaluate(() => requests.length);
  await timeline.locator('[data-room-graph-gate="5"]').click();
  assert.equal(
    await timeline
      .locator('[data-room-graph-gate="5"]')
      .getAttribute("aria-pressed"),
    "true",
  );
  assert.equal(
    await page.evaluate(() => requests.length),
    before,
    "highlighting another gate redraws without fetching",
  );
  assert.equal(
    await graph.locator(".threshold-line.current").count(),
    0,
    "radars without a threshold for that gate draw none",
  );

  // Opening a radar's own history must not blank the room chart, even while
  // the radar's slow history request is still running.
  await page.evaluate(() => {
    window.slowRadarHistory = panel._hass.callWS;
    panel._hass.callWS = async (message) => {
      if (message.type.endsWith("history_series_multi"))
        await new Promise((resolve) => setTimeout(resolve, 1500));
      return slowRadarHistory(message);
    };
  });
  try {
    await timeline.locator('[data-room-device="a"]').click();
    for (const wait of [100, 1000, 2500]) {
      await page.waitForTimeout(wait);
      assert.equal(
        await graph.locator(`[data-room-signal="a"] .gate-line`).count(),
        8,
        `room chart stays drawn ${wait} ms after opening the radar history`,
      );
    }
  } finally {
    await page.evaluate(() => {
      panel._hass.callWS = slowRadarHistory;
    });
  }
};
