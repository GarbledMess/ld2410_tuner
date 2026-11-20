const assert = require("node:assert/strict");
module.exports = async function testSourceLanes(page) {
  const result = await page.evaluate(() => {
    const labels = Array.from({ length: 10000 }, (_, i) => ({
      start: i,
      end: i + 1,
      state: i % 2 ? "present" : "not_present",
    }));
    const start = performance.now();
    const html = panel._presenceLanesSvg(
      { manual: labels, entity: [], guessed: [] },
      { start: 0, end: 10000 },
      760,
    );
    const node = document.createElement("div");
    node.innerHTML = `<svg>${html}</svg>`;
    return {
      count: node.querySelectorAll("*").length,
      elapsed: performance.now() - start,
      text: node.textContent,
    };
  });
  assert.ok(result.count < 30, `bounded DOM: ${result.count}`);
  assert.ok(result.elapsed < 1000, `10,000 segments: ${result.elapsed}ms`);
  console.log(
    `Source lanes: ${result.count} SVG elements for 10,000 segments in ${result.elapsed.toFixed(1)}ms`,
  );
  assert.match(result.text, /Human/);
  assert.match(result.text, /Entity/);
  assert.match(result.text, /Guessed/);
  const checks = await page.evaluate(() => {
    const evidence = {
      manual: [
        { start: -10, end: 5, state: "present" },
        { start: 5, end: 10, state: "present" },
        { start: 90, end: 100, state: "present" },
      ],
      entity: [],
      guessed: [],
    };
    const host = document.createElement("div");
    host.innerHTML = `<svg>${panel._presenceLanesSvg(evidence, { start: 0, end: 10 }, 760)}</svg>${panel._presenceLegendHtml()}`;
    panel.shadowRoot.append(host);
    panel._wirePresenceLanes(host);
    const button = host.querySelector('[data-presence-toggle="manual"]');
    const path = host.querySelector('[data-presence-state="present"]');
    const merged = path.getAttribute("d");
    const originalCall = panel._call;
    let calls = 0;
    panel._call = () => {
      calls++;
      throw new Error("Toggling must not fetch");
    };
    button.click();
    panel._call = originalCall;
    const hidden = host.querySelector('[data-presence-lane="manual"]').style
      .display;
    const pressed = button.getAttribute("aria-pressed");
    const buttonText = button.textContent;
    panel._presenceShown = null;
    const persisted = panel._presenceVisibility().manual;
    button.click();
    const room = panel._roomPresenceEvidence("test", {
      series: [
        {
          name: "Radar A",
          chart: {
            presence_evidence: {
              guessed: [{ start: 1, end: 2, state: "present" }],
            },
          },
        },
      ],
    });
    host.remove();
    return {
      merged,
      hidden,
      pressed,
      buttonText,
      persisted,
      calls,
      members: room.guessedMembers,
    };
  });
  assert.equal(checks.merged, "M34.00,241H750.00");
  assert.equal(checks.calls, 0);
  assert.equal(checks.hidden, "none");
  assert.equal(checks.pressed, "false");
  assert.match(checks.buttonText, /Hidden/);
  assert.equal(checks.persisted, false);
  assert.equal(checks.members[0].name, "Radar A");
  const chartIntegration = await page.evaluate(() => {
    const chart = {
      start: 0,
      end: 10,
      bucket_seconds: 1,
      series: {},
      presence_evidence: {
        manual: [{ start: 0, end: 10, state: "unknown" }],
        entity: [],
        guessed: [],
      },
    };
    const node = document.createElement("div");
    node.innerHTML = panel._buildChartSvg(
      {},
      { kind: "still", gate: 0, data: chart },
      390,
    );
    const radar = node.querySelectorAll("[data-presence-lane]").length;
    node.innerHTML = panel._roomGraphSvg(
      "source-lanes-test",
      { bounds: { start: 0, end: 10 }, series: [] },
      390,
    );
    const room = node.querySelectorAll("[data-presence-lane]").length;
    return {
      radar,
      room,
      buttons: node.querySelectorAll("[data-presence-toggle][aria-pressed]")
        .length,
    };
  });
  assert.deepEqual(chartIntegration, { radar: 3, room: 3, buttons: 3 });
  const emptyHistory = await page.evaluate(() => {
    const node = document.createElement("div");
    panel._renderChartData(
      "source-lanes-test",
      node,
      {},
      {
        gate: 0,
        kind: "still",
        data: {
          start: 0,
          end: 10,
          series: {},
          presence_evidence: {
            manual: [{ start: 0, end: 10, state: "present" }],
            entity: [],
            guessed: [],
          },
        },
      },
      true,
    );
    return {
      lanes: node.querySelectorAll("[data-presence-lane]").length,
      text: node.textContent,
    };
  });
  assert.equal(
    emptyHistory.lanes,
    3,
    "evidence remains visible without gate recordings",
  );
  assert.match(emptyHistory.text, /No recorded history/);
  const legacySensor = await page.evaluate(() => {
    const bounds = panel._historyDayBounds(
      panel._historyStateFor("room:source-lanes-test").day,
    );
    const now = bounds.start + 100;
    const group = {
      device_ids: [],
      labels: [
        { source: "sensor", start: now - 10, end: now, state: "present" },
      ],
    };
    const rooms = panel._data.rooms;
    panel._data.rooms = {
      ...panel._roomsData(),
      groups: { ...panel._roomsData().groups, "source-lanes-test": group },
    };
    let html;
    try {
      html = panel._roomTimelineHtml("source-lanes-test", group);
    } finally {
      panel._data.rooms = rooms;
    }
    const node = document.createElement("div");
    node.innerHTML = html;
    return node.querySelectorAll(".room-sensor-labels").length;
  });
  assert.equal(legacySensor, 0, "sensor evidence uses the toggleable SVG lane");
  const unavailableStorage = await page.evaluate(() => {
    const get = Storage.prototype.getItem;
    const set = Storage.prototype.setItem;
    try {
      Storage.prototype.getItem = () => {
        throw new Error("Unavailable");
      };
      Storage.prototype.setItem = () => {
        throw new Error("Unavailable");
      };
      panel._presenceShown = null;
      const defaults = { ...panel._presenceVisibility() };
      const host = document.createElement("div");
      host.innerHTML = panel._presenceLegendHtml();
      panel.shadowRoot.append(host);
      panel._wirePresenceLanes(host);
      host.querySelector('[data-presence-toggle="entity"]').click();
      const toggled = panel._presenceVisibility().entity;
      host.remove();
      return { defaults, toggled };
    } finally {
      Storage.prototype.getItem = get;
      Storage.prototype.setItem = set;
      panel._presenceShown = null;
    }
  });
  assert.deepEqual(unavailableStorage, {
    defaults: { manual: true, entity: true, guessed: true },
    toggled: false,
  });
};
