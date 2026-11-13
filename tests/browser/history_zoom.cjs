const assert = require("node:assert/strict");

// A day's graph can be zoomed to a shorter window inside that day, for a
// radar's label editor and for a room's presence graph alike.
const DAY = "2026-09-23";
const local = (time) => Date.parse(`${DAY}T${time}+01:00`) / 1000;
const DAY_START = local("00:00:00");
const DAY_END = Date.parse("2026-09-24T00:00:00+01:00") / 1000;

async function zoomState(page, scope, key) {
  return page.evaluate(
    ({ scope, key }) => {
      const root = panel.shadowRoot.querySelector(scope);
      const button = (name) =>
        root.querySelector(`[data-history-zoom="${name}"]`);
      return {
        bounds: panel._historyRangeBounds(key),
        label: root.querySelector("[data-history-zoom-label]").textContent,
        disabled: Object.fromEntries(
          ["out", "in", "earlier", "later", "selection", "day"].map((name) => [
            name,
            button(name).disabled,
          ]),
        ),
      };
    },
    { scope, key },
  );
}

async function testRadarZoom(page, screenshotDir) {
  await page.setViewportSize({ width: 1400, height: 1000 });
  const scope = '[data-device-id="a"]';
  const card = page.locator(scope);
  const settle = () =>
    page.waitForFunction(() => !panel._chartState.get("a").loading);
  await page.evaluate((day) => {
    const card = panel.shadowRoot.querySelector('[data-device-id="a"]');
    panel._setSectionCollapsed("a", "history", false);
    card
      .querySelector('.subsection[data-section="history"]')
      .classList.remove("collapsed");
    panel._changeHistoryDay(card, "a", day);
  }, DAY);
  await settle();
  const zoom = (name) =>
    card.locator(`[data-history-zoom="${name}"]`).first().click();
  const lastChart = () =>
    page.evaluate(() => {
      const call = requests.findLast((r) =>
        r.type.endsWith("history_series_multi"),
      );
      const cs = panel._chartState.get("a");
      return {
        hours: call.hours,
        end: call.end,
        chart: { start: cs.data.start, end: cs.data.end },
      };
    });

  let state = await zoomState(page, scope, "a");
  assert.deepEqual(state.bounds, { start: DAY_START, end: DAY_END });
  assert.equal(state.disabled.out, true, "a whole day cannot zoom out");
  assert.equal(state.disabled.day, true);
  assert.equal(state.disabled.in, false);

  await zoom("in");
  await settle();
  let chart = await lastChart();
  assert.equal(chart.hours, 12, "zooming in halves the window");
  assert.equal(chart.end, local("18:00:00"), "around the window's centre");
  assert.deepEqual(chart.chart, {
    start: local("06:00:00"),
    end: local("18:00:00"),
  });
  state = await zoomState(page, scope, "a");
  assert.deepEqual(
    state.bounds,
    chart.chart,
    "the label bar follows the zoomed graph",
  );
  assert.match(state.label, /06:00.*18:00/);
  assert.equal(state.disabled.out, false);
  for (const width of [1400, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    await card
      .locator('.subsection[data-section="history"]')
      .screenshot({ path: `${screenshotDir}/zoomed-history-${width}.png` });
  }
  await page.setViewportSize({ width: 1400, height: 1000 });

  await zoom("earlier");
  await settle();
  chart = await lastChart();
  assert.deepEqual(chart.chart, {
    start: DAY_START,
    end: local("12:00:00"),
  });
  state = await zoomState(page, scope, "a");
  assert.equal(state.disabled.earlier, true, "the window stays in its day");

  await zoom("later");
  await zoom("later");
  await settle();
  assert.deepEqual((await lastChart()).chart, {
    start: local("12:00:00"),
    end: DAY_END,
  });
  assert.equal((await zoomState(page, scope, "a")).disabled.later, true);

  // A dragged selection becomes the window, with a little context either side.
  await page.evaluate(
    ({ start, end }) => {
      const card = panel.shadowRoot.querySelector('[data-device-id="a"]');
      panel._setHistoryRange(card, "a", { start, end }, true);
    },
    { start: local("14:00:00"), end: local("15:00:00") },
  );
  await zoom("selection");
  await settle();
  chart = await lastChart();
  assert.deepEqual(chart.chart, {
    start: local("13:54:00"),
    end: local("15:06:00"),
  });
  assert.deepEqual(
    await page.evaluate(() => panel._chartState.get("a").selection),
    { start: local("14:00:00"), end: local("15:00:00") },
    "zooming keeps the selection",
  );

  for (let step = 0; step < 3; step++) await zoom("in");
  await settle();
  chart = await lastChart();
  assert.equal(
    chart.chart.end - chart.chart.start,
    600,
    "the shortest window is ten minutes",
  );
  assert.equal((await zoomState(page, scope, "a")).disabled.in, true);
  const ticks = await card
    .locator(".chart-canvas svg text.axis-label")
    .allTextContents();
  assert.ok(
    ticks.some((tick) => /\d\d:\d\d:\d\d/.test(tick)),
    `short windows label seconds: ${ticks}`,
  );

  await zoom("day");
  await settle();
  chart = await lastChart();
  assert.equal(chart.hours, 24);
  assert.deepEqual(chart.chart, { start: DAY_START, end: DAY_END });

  await zoom("in");
  await settle();
  await card.locator('[data-action="history-next"]').click();
  await settle();
  assert.equal(
    (await lastChart()).hours,
    24,
    "another day opens at its whole length",
  );
  await page.evaluate((day) => {
    const card = panel.shadowRoot.querySelector('[data-device-id="a"]');
    panel._newHistoryPeriod(card, "a");
    panel._changeHistoryDay(card, "a", day);
  }, DAY);
  await settle();
}

async function testRoomZoom(page, screenshotDir) {
  const gid = "area:ux";
  const rid = `room:${gid}`;
  const scope = `[data-room-timeline="${gid}"]`;
  const section = page.locator(scope);
  const settle = () =>
    page.waitForFunction((gid) => !panel._roomGraphState(gid).loading, gid);
  await page.evaluate(
    async ({ gid, day }) => {
      panel._historyStateFor(`room:${gid}`).day = day;
      panel._drawLayout();
      await panel._loadRoomGraph(gid, panel._roomsData().groups[gid], true);
    },
    { gid, day: DAY },
  );
  await settle();
  const zoom = async (name) => {
    await section.locator(`[data-history-zoom="${name}"]`).click();
    await settle();
  };
  const lastRoom = () =>
    page.evaluate((gid) => {
      const call = requests.findLast((r) => r.type.endsWith("room_series"));
      return {
        hours: call.hours,
        end: call.end,
        drawn: panel._roomGraphState(gid).data.bounds,
      };
    }, gid);

  let state = await zoomState(page, scope, rid);
  assert.deepEqual(state.bounds, { start: DAY_START, end: DAY_END });
  assert.equal(state.disabled.out, true);

  await zoom("in");
  let room = await lastRoom();
  assert.equal(room.hours, 12);
  assert.equal(room.end, local("18:00:00"));
  assert.deepEqual(room.drawn, {
    start: local("06:00:00"),
    end: local("18:00:00"),
  });
  state = await zoomState(page, scope, rid);
  assert.deepEqual(state.bounds, room.drawn, "the label bar follows the graph");
  assert.match(state.label, /06:00.*18:00/);
  const ticks = await section
    .locator("[data-room-graph] svg text.axis-label")
    .allTextContents();
  // Each tick holds a zoned hover title before its visible clock time.
  for (const time of ["06:00", "18:00"])
    assert.ok(
      ticks.some((tick) => tick.endsWith(`BST${time}`)),
      `${time} in ${ticks}`,
    );
  assert.match(
    await section.locator(".history-axis").innerText(),
    /06:00[\s\S]*18:00/,
  );

  await section.screenshot({ path: `${screenshotDir}/zoomed-room.png` });

  // Polls redraw the room but keep its zoom.
  await page.evaluate(() => panel._load(true));
  await settle();
  assert.deepEqual((await zoomState(page, scope, rid)).bounds, room.drawn);

  await page.evaluate(
    ({ rid, start, end }) => {
      const section = panel._roomGraphSection(rid.slice(5));
      panel._setHistoryRange(section, rid, { start, end });
    },
    { rid, start: local("10:00:00"), end: local("11:00:00") },
  );
  await zoom("selection");
  room = await lastRoom();
  assert.deepEqual(room.drawn, {
    start: local("09:54:00"),
    end: local("11:06:00"),
  });
  const overlays = await page.evaluate((gid) => {
    const section = panel._roomGraphSection(gid);
    const rect = (selector) => {
      const box = section.querySelector(selector).getBoundingClientRect();
      return [box.left, box.right];
    };
    return {
      graph: rect("[data-room-graph-selection]"),
      bar: rect(".history-range-selection"),
    };
  }, gid);
  overlays.graph.forEach((value, index) =>
    assert.ok(
      Math.abs(value - overlays.bar[index]) < 1.5,
      `graph and bar selections align: ${JSON.stringify(overlays)}`,
    ),
  );

  await zoom("day");
  room = await lastRoom();
  assert.deepEqual(
    room.drawn,
    { start: DAY_START, end: DAY_END },
    "the whole day returns, from cache when already loaded",
  );

  await zoom("in");
  await section.locator('[data-room-label-action="next"]').click();
  await settle();
  assert.equal(
    (await lastRoom()).hours,
    24,
    "another day opens at its whole length",
  );
  await page.evaluate(
    ({ gid, day }) => {
      panel._historyStateFor(`room:${gid}`).day = day;
      panel._drawLayout();
    },
    { gid, day: DAY },
  );
  await settle();
}

module.exports = { testRadarZoom, testRoomZoom };
