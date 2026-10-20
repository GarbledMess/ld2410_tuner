const assert = require("node:assert/strict");
const path = require("node:path");

module.exports = async function testCardConsistency(page, screenshotDir) {
  await page.evaluate(() => {
    window.consistencyOriginal = structuredClone(fixture);
    window.consistencyCallWS = panel._hass.callWS;
    panel._hass.callWS = async (message) => {
      if (!message.type.endsWith("history_series_multi"))
        return consistencyCallWS(message);
      requests.push(message);
      const end = message.end ?? Date.now() / 1000;
      const start = end - message.hours * 3600;
      return {
        start,
        end,
        bucket_seconds: 300,
        labels: [],
        series: Object.fromEntries(
          message.keys.map((key, gate) => [
            key,
            {
              points: Array.from(
                { length: Math.floor((end - start) / 300) },
                (_, index) => {
                  const phase = index / 288;
                  const offset = message.device_id === "a" ? 0 : 0.08;
                  const active =
                    (phase > 0.22 + offset && phase < 0.4 + offset) ||
                    (phase > 0.64 && phase < 0.85);
                  const value = Math.round(
                    Math.max(
                      2,
                      Math.min(
                        100,
                        (active ? 54 : 9) +
                          8 * Math.sin(index / 4 + offset * 30) +
                          5 * Math.cos(index / 11) -
                          gate,
                      ),
                    ),
                  );
                  return {
                    t: start + index * 300 + 150,
                    min: Math.max(0, value - 4),
                    avg: value,
                    max: Math.min(100, value + 4),
                  };
                },
              ).filter((point, index) => index < 145 || index > 155),
            },
          ]),
        ),
      };
    };
    const group = (name, kind, ids) => ({
      name,
      kind,
      area_id: "ux",
      device_ids: ids,
      labels: [],
      automatic: true,
      assessment: { state: "pending" },
    });
    fixture.rooms = {
      areas: { ux: "Office" },
      members: {
        a: { name: "Desk radar", area_id: "ux", recording_enabled: true },
        b: { name: "Door radar", area_id: "ux", recording_enabled: true },
      },
      groups: {
        "area:ux": group("Office", "area", ["a", "b"]),
        "zone:ux": group("Desk", "zone", ["a", "b"]),
      },
    };
    fixture.layout = [
      {
        id: "area:ux",
        name: "Office",
        kind: "area",
        group_id: "area:ux",
        devices: [],
        children: [
          {
            id: "zone:ux",
            name: "Desk",
            kind: "zone",
            group_id: "zone:ux",
            devices: ["a", "b"],
            children: [],
          },
        ],
      },
    ];
    fixture.learning_sets = {};
    panel._groupOpen.add("area:ux");
    panel._groupOpen.add("zone:ux");
    panel._historyStateFor("room:area:ux").day = "2026-09-23";
    panel._historyStateFor("room:zone:ux").day = "2026-09-23";
    return panel._load(true);
  });
  const area = page.locator('[data-node-id="area:ux"]');
  const zone = page.locator('[data-leaf-id="zone:ux"]');
  assert.equal(
    await area.evaluate((node) => node.open),
    false,
    "new rooms start closed",
  );
  assert.equal(
    await zone.evaluate((node) => node.open),
    false,
    "new zones start closed",
  );
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await area.evaluate((node) => node.scrollIntoView({ block: "start" }));
    await page.screenshot({
      path: path.join(screenshotDir, `default-overview-${width}.png`),
    });
  }
  await page.setViewportSize({ width: 1400, height: 1000 });
  await area.locator(":scope > summary").click();
  await zone.locator(":scope > summary").click();
  assert.deepEqual(
    await page.evaluate(() =>
      [...panel.shadowRoot.querySelectorAll("summary, .subsection-head")]
        .filter(
          (node) =>
            !node.querySelector(".section-description")?.textContent.trim(),
        )
        .map((node) => node.textContent),
    ),
    [],
    "room, zone and device disclosures all describe their contents",
  );
  for (const element of [area, zone, page.locator("[data-layout-footer]")])
    assert.equal(
      await element.evaluate((node) => node.matches(".panel-card")),
      true,
      "room, zone and management controls each have a card",
    );
  assert.equal(
    await page
      .locator("#snapshot-status")
      .evaluate((node) => !!node.closest(".panel-card")),
    true,
    "status stays inside a card",
  );
  const timeline = area.locator('[data-room-timeline="area:ux"]');
  const graph = timeline.locator("[data-room-graph]");
  await graph.locator("svg").waitFor();
  assert.equal(
    await graph.locator("polyline").count(),
    4,
    "both radars show continuous history split at a real recording outage",
  );
  assert.equal(
    await graph.locator("[data-room-signal]").count(),
    2,
    "room plot includes both member radars",
  );
  assert.match(await timeline.innerText(), /signal/i);
  const before = await page.evaluate(
    () =>
      requests.filter((r) => r.type.endsWith("history_series_multi")).length,
  );
  await page.evaluate(() => panel._draw());
  await graph.locator("svg").waitFor();
  assert.equal(
    await page.evaluate(
      () =>
        requests.filter((r) => r.type.endsWith("history_series_multi")).length,
    ),
    before,
    "polling reuses the room graph",
  );
  const hit = graph.locator("[data-room-plot-hit]");
  await hit.scrollIntoViewIfNeeded();
  const box = await hit.boundingBox();
  await page.mouse.move(box.x + box.width * 0.25, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * 0.5, box.y + box.height / 2, {
    steps: 5,
  });
  await page.mouse.up();
  assert.match(
    await timeline.locator('[data-action="history-start"]').inputValue(),
    /T06:00/,
  );
  assert.match(
    await timeline.locator('[data-action="history-end"]').inputValue(),
    /T12:00/,
  );
  const radar = zone.locator('[data-device-id="a"]');
  await radar.locator('[data-action="open-history"]').click();
  await page.evaluate(async () => {
    const card = panel.shadowRoot.querySelector(
      '[data-leaf-id="zone:ux"] [data-device-id="a"]',
    );
    const state = panel._chartState.get("a");
    state.kind = "still";
    state.cache?.clear();
    panel._historyStateFor("a").day = "2026-09-23";
    panel._showHistoryWindow(card, "a", panel._historyDayBounds("2026-09-23"));
    await panel._fetchChartData("a", true);
  });
  assert.ok(
    (await radar.locator(".value-line.active").first().getAttribute("d")).split(
      "L",
    ).length > 100,
    "radar history renders the populated fixture rather than an earlier test's sparse cache",
  );
  assert.equal(
    await radar
      .locator(".body")
      .evaluate((node) => node.classList.contains("collapsed")),
    false,
  );
  assert.equal(
    await radar
      .locator('[data-section="history"].subsection')
      .evaluate((node) => node.classList.contains("collapsed")),
    false,
  );
  await radar.locator('[data-action="open-recommendations"]').click();
  assert.equal(
    await radar
      .locator('[data-section="details"].subsection')
      .evaluate((node) => node.classList.contains("collapsed")),
    false,
  );
  for (const width of [1440, 768, 390, 320]) {
    await page.setViewportSize({ width, height: 900 });
    await area.evaluate((node) => node.scrollIntoView({ block: "start" }));
    assert.ok(
      (await page.evaluate(() => document.documentElement.scrollWidth)) <=
        width,
      `no horizontal overflow at ${width}px`,
    );
    assert.equal(
      await zone.evaluate(
        (node) => node.closest("[data-node-id]").dataset.nodeId,
      ),
      "area:ux",
    );
    assert.equal(
      await radar.evaluate(
        (node) => node.closest("[data-leaf-id]").dataset.leafId,
      ),
      "zone:ux",
    );
    // Read every owner/child in the same frame: shortcuts scroll smoothly, so
    // separate protocol round-trips can otherwise compare different scroll offsets.
    const [roomBounds, zoneBounds, radarBounds] = await page.evaluate(() => {
      const root = panel.shadowRoot;
      return [
        '[data-node-id="area:ux"]',
        '[data-leaf-id="zone:ux"]',
        '[data-leaf-id="zone:ux"] [data-device-id="a"]',
      ].map((selector) => {
        const r = root.querySelector(selector).getBoundingClientRect();
        return { x: r.x, y: r.y, width: r.width, height: r.height };
      });
    });
    for (const [outer, inner] of [
      [roomBounds, zoneBounds],
      [zoneBounds, radarBounds],
    ]) {
      assert.ok(
        inner.x >= outer.x &&
          inner.y >= outer.y &&
          inner.x + inner.width <= outer.x + outer.width + 1 &&
          inner.y + inner.height <= outer.y + outer.height + 1,
        `child stays inside its owning card at ${width}px: ${JSON.stringify({ outer, inner })}`,
      );
    }
    await page.screenshot({
      path: path.join(screenshotDir, `room-overview-${width}.png`),
    });
    if ([1440, 390].includes(width)) {
      await page.waitForFunction(() => {
        const svg = panel.shadowRoot.querySelector(
          '[data-leaf-id="zone:ux"] [data-device-id="a"] .chart-canvas svg',
        );
        return (
          svg &&
          svg.getBoundingClientRect().height >= 160 &&
          svg.querySelector(".axis-label").getBoundingClientRect().height >= 9
        );
      });
      await zone.evaluate((node) => node.scrollIntoView({ block: "start" }));
      await page.screenshot({
        path: path.join(screenshotDir, `zone-overview-${width}.png`),
      });
      await radar.evaluate((node) => node.scrollIntoView({ block: "start" }));
      await page.screenshot({
        path: path.join(screenshotDir, `radar-overview-${width}.png`),
      });
      await radar
        .locator('[data-section="details"].subsection')
        .evaluate((node) => node.scrollIntoView({ block: "start" }));
      await page.screenshot({
        path: path.join(screenshotDir, `radar-recommendations-${width}.png`),
      });
      await radar
        .locator(".chart-canvas")
        .evaluate((node) => node.scrollIntoView({ block: "start" }));
      await page.screenshot({
        path: path.join(screenshotDir, `radar-history-${width}.png`),
      });
    }
  }
  await page.evaluate(() => {
    fixture.rooms.groups["area:ux"].device_ids = ["a"];
    return panel._load(true);
  });
  await page.waitForFunction(
    () => panel._roomCharts.get("area:ux").data.series.length === 1,
  );
  const liveRefresh = await page.evaluate(async () => {
    const now = Date.now;
    const state = panel._roomCharts.get("area:ux");
    const before = state.version;
    Date.now = () => (state.data.bounds.start + 3600) * 1000;
    state.loadedAt = Date.now() - 16000;
    try {
      await panel._loadRoomGraph("area:ux", fixture.rooms.groups["area:ux"]);
      return state.version > before;
    } finally {
      Date.now = now;
    }
  });
  assert.equal(
    liveRefresh,
    true,
    "current-day graph expires even when labels are unchanged",
  );
  await page.setViewportSize({ width: 1400, height: 1000 });
  await page.evaluate(() => {
    fixture = consistencyOriginal;
    panel._hass.callWS = consistencyCallWS;
    panel._groupOpen.delete("area:ux");
    panel._groupOpen.delete("zone:ux");
    return panel._load(true);
  });
  assert.equal(
    await page.evaluate(() => panel._roomCharts.has("area:ux")),
    false,
    "removed rooms release graph caches and observers",
  );
  assert.equal(
    await page.evaluate(async () => {
      const original = panel._call;
      let attempts = 0;
      panel._call = async (method, parameters, ...rest) => {
        if (parameters?.device_id !== "busy-test")
          return original.call(panel, method, parameters, ...rest);
        attempts++;
        if (attempts < 3)
          throw Object.assign(new Error("Busy"), { code: "busy" });
        return { series: {} };
      };
      try {
        await panel._chartCall({ device_id: "busy-test" });
        return attempts;
      } finally {
        panel._call = original;
      }
    }),
    3,
    "chart saturation retries automatically within a fixed bound",
  );
};
