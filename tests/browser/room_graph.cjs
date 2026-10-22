const assert = require("node:assert/strict");

module.exports = async function testRoomGraphResilience(page) {
  await page.waitForFunction(
    () => panel._activeCharts === 0 && !panel._chartQueue.length,
  );
  const outcomes = await page.evaluate(async () => {
    const gid = "area:ux",
      group = panel._roomsData().groups[gid];
    const original = panel._hass.callWS;
    const calls = [];
    let failed = new Set(["b"]),
      empty = false;
    panel._hass.callWS = async (message) => {
      if (!message.type.endsWith("history_series_multi"))
        return original(message);
      calls.push({ ...message });
      if (failed.has(message.device_id))
        throw new Error("Synthetic radar failure");
      const value = structuredClone(await original(message));
      if (empty) value.points = [];
      else if (value.points?.length) value.points[0].avg = 77;
      return value;
    };
    try {
      await panel._loadRoomGraph(gid, group, true);
      const state = panel._roomGraphState(gid);
      const section = panel._roomGraphSection(gid);
      const partial = {
        ids: state.data.series.map((s) => s.id),
        first: state.data.series.find((s) => s.id === "a").points[0].avg,
        text: section.innerText,
      };
      const before = calls.length;
      state.loadedAt = 0;
      await panel._loadRoomGraph(gid, group);
      const polled = calls.length;
      failed.clear();
      await panel._loadRoomGraph(gid, group, true);
      const retried = calls.slice(before);
      const recovered = {
        text: section.innerText,
        ids: state.data.series.map((s) => s.id),
      };
      failed = new Set(["a", "b"]);
      await panel._loadRoomGraph(gid, group, true);
      state.loadedAt = 0;
      const allFailedCount = calls.length;
      await panel._loadRoomGraph(gid, group);
      const allFailed = {
        text: section.innerText,
        graph: !!section.querySelector("svg"),
        retried: calls.length !== allFailedCount,
      };
      failed.clear();
      await panel._loadRoomGraph(gid, group, true);
      state.kind = "move";
      failed.add("b");
      await panel._loadRoomGraph(gid, group, false, true);
      const freshPartial = state.data.series.map((s) => s.id);
      state.kind = "all";
      const beforeCache = calls.length;
      await panel._loadRoomGraph(gid, group, false, true);
      const cachedBack = {
        ids: state.data.series.map((s) => s.id),
        calls: calls.length - beforeCache,
      };
      state.kind = "move";
      await panel._loadRoomGraph(gid, group, false, true);
      const beforeRepeated = calls.length;
      await panel._loadRoomGraph(gid, group, true);
      const repeated = calls
        .slice(beforeRepeated)
        .map((call) => call.device_id);
      state.kind = "still";
      failed.add("a");
      await panel._loadRoomGraph(gid, group, false, true);
      const wrongKindGraph = !!section.querySelector("svg");
      const ui = panel._historyStateFor(`room:${gid}`);
      const oldDay = ui.day;
      ui.day = "2026-09-22";
      await panel._loadRoomGraph(gid, group, false, true);
      const wrongDayGraph = !!section.querySelector("svg");
      ui.day = oldDay;
      state.kind = "all";
      failed.clear();
      await panel._loadRoomGraph(gid, group, true);
      empty = true;
      await panel._loadRoomGraph(gid, group, true);
      const emptySuccess = section.innerText;
      failed = new Set(["a", "b"]);
      await panel._loadRoomGraph(gid, group, true);
      const emptyFailure = section.innerText;
      empty = false;
      failed.clear();
      await panel._loadRoomGraph(gid, group, true);
      return {
        emptySuccess,
        emptyFailure,
        freshPartial,
        cachedBack,
        repeated,
        wrongKindGraph,
        wrongDayGraph,
        partial,
        before,
        polled,
        retried,
        recovered,
        allFailed,
        originalEnd: calls[0].end,
      };
    } finally {
      panel._hass.callWS = original;
    }
  });
  assert.match(
    outcomes.emptySuccess,
    /No recorded signals in this period/,
    "a successful empty result is explained",
  );
  assert.doesNotMatch(
    outcomes.emptyFailure,
    /No recorded signals/,
    "a failed refresh removes stale empty-history wording",
  );
  assert.deepEqual(
    outcomes.freshPartial,
    ["a"],
    "a new signal kind shows successful data without requiring a complete prior graph",
  );
  assert.deepEqual(
    outcomes.cachedBack,
    { ids: ["a", "b"], calls: 0 },
    "back navigation restores the complete cached kind after a partial failure",
  );
  assert.deepEqual(
    outcomes.repeated,
    ["b"],
    "repeated failed retries do not discard successful radar data",
  );
  assert.equal(
    outcomes.wrongKindGraph,
    false,
    "failed navigation never shows signals from another kind",
  );
  assert.equal(
    outcomes.wrongDayGraph,
    false,
    "failed navigation never shows signals from another day",
  );
  assert.equal(
    outcomes.partial.first,
    77,
    "fresh successful radar signals survive another radar failure",
  );
  assert.deepEqual(
    outcomes.partial.ids,
    ["a", "b"],
    "failed refresh keeps previous signals alongside fresh ones",
  );
  assert.match(outcomes.partial.text, /Room b.*Synthetic radar failure/);
  assert.match(
    outcomes.partial.text,
    /previous/i,
    "stale retained signals are identified",
  );
  assert.equal(
    outcomes.before,
    outcomes.polled,
    "a partial failure does not create a polling retry loop",
  );
  assert.deepEqual(
    outcomes.retried.map((call) => call.device_id),
    ["b"],
    "explicit retry requests only the failed radar",
  );
  assert.ok(
    outcomes.retried.every((call) => call.end === outcomes.originalEnd),
    "partial retries keep the original exact window endpoint",
  );
  assert.doesNotMatch(outcomes.recovered.text, /Synthetic radar failure/);
  assert.deepEqual(outcomes.recovered.ids, ["a", "b"]);
  assert.equal(
    outcomes.allFailed.graph,
    true,
    "a failed refresh retains the previous graph",
  );
  assert.equal(
    outcomes.allFailed.retried,
    false,
    "unchanged failed requests wait for explicit retry",
  );
  assert.doesNotMatch(
    outcomes.allFailed.text,
    /No recorded signals for this day/,
    "errors do not imply missing history",
  );
  await page.evaluate(() =>
    panel._loadRoomGraph("area:ux", panel._roomsData().groups["area:ux"], true),
  );

  const inFlight = await page.evaluate(async () => {
    const gid = "area:ux",
      group = panel._roomsData().groups[gid];
    const state = panel._roomGraphState(gid);
    const area = panel.shadowRoot.querySelector('[data-node-id="area:ux"]');
    const original = panel._hass.callWS;
    let release;
    const gate = new Promise((resolve) => (release = resolve));
    panel._hass.callWS = async (message) => {
      if (message.summary && message.device_id === "b") await gate;
      return original(message);
    };
    const oldKind = state.kind;
    state.kind = "move";
    const work = panel._loadRoomGraph(gid, group, false, true);
    try {
      for (
        let frame = 0;
        frame < 100 &&
        !(
          state.loadedKind === "move" &&
          state.data?.series.some((item) => item.id === "a")
        );
        frame++
      )
        await new Promise(requestAnimationFrame);
      const progressive =
        state.loading &&
        state.loadedKind === "move" &&
        !!panel._roomGraphSection(gid).querySelector("svg");
      const request = state.request;
      area.open = false;
      panel._layoutOpen.set(gid, false);
      await panel._load(true);
      release();
      await work;
      const completed = !state.loading && state.data.series.length === 2;
      area.open = true;
      panel._layoutOpen.set(gid, true);
      await new Promise(requestAnimationFrame);
      await new Promise(requestAnimationFrame);
      return { progressive, completed, unchanged: state.request === request };
    } finally {
      release();
      await work;
      panel._hass.callWS = original;
      state.kind = oldKind;
      await panel._loadRoomGraph(gid, group, false, true);
    }
  });
  assert.equal(
    inFlight.progressive,
    true,
    "fast radar results render while another radar is still loading",
  );
  assert.equal(
    inFlight.completed,
    true,
    "closing the room does not cancel accepted chart work",
  );
  assert.equal(
    inFlight.unchanged,
    true,
    "reopening a room reuses its completed in-flight request",
  );

  const area = page.locator('[data-node-id="area:ux"]');
  await area.locator(":scope > summary").click();
  const hidden = await page.evaluate(async () => {
    const original = Date.now;
    const state = panel._roomGraphState("area:ux");
    Date.now = () => (state.data.bounds.start + 3600) * 1000;
    try {
      state.loadedAt = 0;
      panel._roomGraphState("zone:ux").loadedAt = 0;
      const before = requests.filter((r) =>
        r.type.endsWith("history_series_multi"),
      ).length;
      await panel._load(true);
      const after = requests.filter((r) =>
        r.type.endsWith("history_series_multi"),
      ).length;
      return { before, after };
    } finally {
      Date.now = original;
    }
  });
  assert.equal(
    hidden.before,
    hidden.after,
    "closed parent rooms do not enqueue automatic graph requests",
  );
  const before = await page.evaluate(
    () =>
      requests.filter((r) => r.type.endsWith("history_series_multi")).length,
  );
  await page.evaluate(() => {
    panel._roomGraphState("area:ux").loadedKey = null;
    panel._roomGraphState("area:ux").cache.clear();
  });
  await area.locator(":scope > summary").click();
  await page.waitForFunction(
    (count) =>
      requests.filter((r) => r.type.endsWith("history_series_multi")).length >
      count,
    before,
  );
  await page.waitForFunction(() => !panel._roomGraphState("area:ux").loading);
  const marked = await area
    .locator('[data-group-id="area:ux"] > summary')
    .innerText();
  assert.match(
    marked,
    /Not assessed/,
    "unavailable assessment summaries explain their state",
  );
  const visual = await page.evaluate(() => {
    const data = {
      bounds: { start: 0, end: 120 },
      series: [
        {
          id: "a",
          name: "Radar",
          color: "#e53935",
          bucket: 60,
          points: [
            { t: 0, avg: 20, min: 20, max: 100 },
            { t: 60, avg: 20, min: 20, max: 20 },
          ],
        },
      ],
    };
    const element = document.createElement("div");
    element.innerHTML = panel._roomGraphSvg("area:ux", data, 500);
    return {
      line: !!element.querySelector(".value-line.active"),
      band: !!element.querySelector(".value-band"),
      polyline: !!element.querySelector("polyline"),
    };
  });
  assert.deepEqual(
    visual,
    { line: true, band: true, polyline: false },
    "room and radar plots share the mean line and min/max band renderer",
  );
  const paused = await page.evaluate(() => {
    const original = fixture.devices.a.recording_enabled;
    fixture.devices.a.recording_enabled = false;
    panel._data.devices.a.recording_enabled = false;
    const state = panel._roomGraphState("area:ux");
    const old = state.data;
    state.data = {
      ...old,
      series: old.series.map((item) => ({ ...item, points: [] })),
    };
    state.error = null;
    state.loading = false;
    state.version++;
    panel._renderRoomGraph("area:ux");
    const text = panel._roomGraphSection("area:ux").innerText;
    fixture.devices.a.recording_enabled = original;
    panel._data.devices.a.recording_enabled = original;
    state.data = old;
    state.version++;
    panel._renderRoomGraph("area:ux");
    return text;
  });
  assert.match(
    paused,
    /Recording paused.*Record data/s,
    "empty room graphs explain paused recording and how to resume it",
  );
};
