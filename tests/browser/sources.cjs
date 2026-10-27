const assert = require("node:assert/strict");
const path = require("node:path");
const testSourcePicker = require("./source_picker.cjs");

module.exports = async function testSources(page, screenshotDir) {
  await page.evaluate(async () => {
    window.beforeSources = structuredClone(fixture);
    fixture.rooms = {
      groups: {},
      members: {},
      ...fixture.rooms,
      areas: { office: "Office", bedroom: "Bedroom" },
    };
    window.sourcesWS = panel._hass.callWS;
    window.beforeSourceStates = panel._hass.states;
    panel._hass.states = {
      "binary_sensor.camera_person": {
        state: "on",
        attributes: { friendly_name: "Camera person" },
      },
      "sensor.phone_area": {
        state: "Bedroom",
        attributes: { area_id: "bedroom", area_name: "Bedroom" },
      },
    };
    panel._hass.callWS = async (message) => {
      if (message.type.endsWith("/configure_presence_sources")) {
        requests.push(message);
        if (window.failSources) throw new Error("Source configuration failed");
        fixture.devices[message.device_id].presence_sources = structuredClone(
          message.settings,
        );
        return message.settings;
      }
      return sourcesWS(message);
    };
    panel._collapsed.delete("a");
    panel._setSectionCollapsed("a", "training", false);
    await panel._load();
  });
  const card = page.locator('[data-device-id="a"]');
  const form = card.locator(".presence-sources");
  await form.locator("summary").click();
  const startBuffer = form.locator(
    '[data-source-buffer="start_buffer_seconds"]',
  );
  const endBuffer = form.locator('[data-source-buffer="end_buffer_seconds"]');
  assert.equal(await startBuffer.inputValue(), "10");
  assert.equal(await endBuffer.inputValue(), "10");

  assert.equal(await form.locator("[data-source-negative]").isChecked(), false);
  await form.locator('[data-action="source-add"]').click();
  await testSourcePicker(page, screenshotDir);
  await form
    .locator("[data-source-entity]")
    .fill("binary_sensor.camera_person");
  await form.locator('[data-action="source-add"]').click();
  assert.deepEqual(
    await form
      .locator("[data-source-kind]")
      .nth(1)
      .locator("option")
      .evaluateAll((options) => options.map((option) => option.value)),
    ["boolean"],
    "new radar sources use the central Bermuda section",
  );
  assert.match(await form.innerText(), /Bermuda section/);
  await form
    .locator("[data-source-entity]")
    .nth(1)
    .fill("binary_sensor.watch_present");
  assert.equal(await form.locator("[data-source-area]").count(), 0);
  assert.equal(await form.locator("[data-source-max-distance]").count(), 0);
  assert.deepEqual(
    await page.evaluate(() =>
      ["bEdRoOm", "old <area>", "office"].map((area) => {
        const container = document.createElement("div");
        container.innerHTML = panel._sourceAreaHtml(area, "Own area");
        const select = container.querySelector("select");
        return {
          value: select.value,
          text: select.selectedOptions[0].textContent,
        };
      }),
    ),
    [
      { value: "bedroom", text: "Bedroom" },
      { value: "old <area>", text: "old <area> (unknown area)" },
      { value: "office", text: "Office" },
    ],
    "central area picker preserves migrated area names and unknown text",
  );
  assert.deepEqual(
    await page.evaluate(() => {
      const source = {
        entity_id: "sensor.phone_area",
        kind: "bermuda",
        max_distance: 3,
      };
      return [
        { present: true, distance: 2.3 },
        { present: false, distance: 4.1 },
        { present: null, distance: null },
        // Outside the area, or no usable area: the backend sends no distance.
        { present: false },
        { present: null },
      ].map((reading) => {
        const container = document.createElement("div");
        container.innerHTML = panel._sourceReadingHtml({
          ...source,
          ...reading,
        });
        return container.textContent;
      });
    }),
    [
      "sensor.phone_area: Present (2.3 m of 3 m)",
      "sensor.phone_area: Too far (4.1 m of 3 m)",
      "sensor.phone_area: No usable distance",
      "sensor.phone_area: Off / outside target area",
      "sensor.phone_area: No usable state",
    ],
  );
  assert.deepEqual(
    await page.evaluate(() =>
      [false, true].map((room) => {
        const container = document.createElement("div");
        container.innerHTML = panel._sourceRowHtml(
          "a",
          { kind: "boolean", entity_id: "", area: "" },
          0,
          room,
        );
        return [...container.querySelectorAll("[data-source-kind] option")].map(
          (o) => o.value,
        );
      }),
    ),
    [["boolean"], ["boolean"]],
    "both editors only offer Boolean",
  );
  assert.deepEqual(
    await page.evaluate(() => {
      const context = {
        _roomsData: () => ({ areas: { street: "Straße", island: "ı" } }),
        _esc: panel._esc,
      };
      return ["STRASSE", "ẞtraße", "i"].map((name) => {
        const container = document.createElement("div");
        container.innerHTML = panel._sourceAreaHtml.call(context, name);
        return container.querySelector("select").value;
      });
    }),
    ["street", "ẞtraße", "i"],
    "legacy names use Unicode case folding without merging dotless i",
  );
  await page.evaluate(() => panel.shadowRoot.activeElement.blur());
  await page.evaluate(() => panel._load());
  assert.equal(
    await form.locator("[data-source-entity]").nth(1).inputValue(),
    "binary_sensor.watch_present",
  );
  assert.equal(await form.locator("[data-source-negative]").isChecked(), false);
  await form.locator('[data-action="sources-save"]').click();
  await page.waitForFunction(() => panel._activeActions === 0);
  assert.deepEqual(
    await page.evaluate(() => fixture.devices.a.presence_sources),
    {
      sources: [
        { entity_id: "binary_sensor.camera_person", kind: "boolean", area: "" },
        {
          entity_id: "binary_sensor.watch_present",
          kind: "boolean",
          area: "",
        },
      ],
      mark_not_present: false,
      confidence: 90,
      start_buffer_seconds: 10,
      end_buffer_seconds: 10,
    },
  );
  await form.locator("[data-source-negative]").check();
  await form.locator("[data-source-confidence]").fill("85");
  await startBuffer.fill("12.5");
  await endBuffer.fill("8");
  await page.evaluate(() => panel.shadowRoot.activeElement.blur());
  await page.evaluate(() => panel._load());
  assert.equal(await startBuffer.inputValue(), "12.5");
  assert.equal(await endBuffer.inputValue(), "8");
  await page.evaluate(() => {
    window.failSources = true;
  });
  await form.locator('[data-action="sources-save"]').click();
  await page.waitForFunction(() => panel._activeActions === 0);
  assert.match(
    await page.locator("#error").innerText(),
    /Source configuration failed/,
  );
  assert.equal(await form.locator("[data-source-negative]").isChecked(), true);
  assert.equal(
    await page.evaluate(
      () => fixture.devices.a.presence_sources.mark_not_present,
    ),
    false,
  );
  await page.evaluate(() => {
    window.failSources = false;
  });
  await form.locator('[data-action="sources-save"]').click();
  await page.waitForFunction(() => panel._activeActions === 0);
  assert.equal(
    await page.evaluate(
      () => fixture.devices.a.presence_sources.mark_not_present,
    ),
    true,
  );
  assert.equal(
    await page.evaluate(() =>
      Object.hasOwn(
        fixture.devices.a.presence_sources.sources[1],
        "max_distance",
      ),
    ),
    false,
  );
  assert.equal(
    await page.evaluate(
      () => fixture.devices.a.presence_sources.start_buffer_seconds,
    ),
    12.5,
  );
  assert.equal(
    await page.evaluate(
      () => fixture.devices.a.presence_sources.end_buffer_seconds,
    ),
    8,
  );
  await page.evaluate(async () => {
    Object.assign(fixture.devices.a.presence_sources, {
      buffering: true,
      state: "unknown",
      raw_state: "present",
    });
    await panel._load();
  });
  assert.match(await form.innerText(), /Buffering presence/);
  assert.doesNotMatch(await form.innerText(), /radar guessing continues/);
  await page.setViewportSize({ width: 390, height: 844 });
  // Resizing returns before ResizeObserver repaints the charts above this form.
  // Check the rendered chart widths as well as mobile fields and pending loads.
  await page.waitForFunction(() => {
    const form = panel.shadowRoot.querySelector(
      '[data-device-id="a"] .presence-sources',
    );
    const start = form.querySelector(
      '[data-source-buffer="start_buffer_seconds"]',
    );
    const end = form.querySelector('[data-source-buffer="end_buffer_seconds"]');
    const first = start.getBoundingClientRect();
    const last = end.getBoundingClientRect();
    const chartsResized = [
      ...panel.shadowRoot.querySelectorAll("[data-chart-canvas]"),
    ].every((canvas) => {
      const svg = canvas.querySelector(":scope > svg");
      if (!canvas.clientWidth || !svg) return true;
      const minimum = canvas.hasAttribute("data-room-graph") ? 320 : 240;
      return (
        svg.viewBox.baseVal.width ===
        Math.max(minimum, Math.round(canvas.clientWidth))
      );
    });
    return (
      window.innerWidth === 390 &&
      first.width > 0 &&
      last.top >= first.bottom &&
      chartsResized &&
      !panel._loading &&
      panel._activeCharts === 0 &&
      panel._chartQueue.length === 0
    );
  });
  await form.screenshot({
    path: path.join(screenshotDir, "presence-sources-mobile.png"),
  });
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
    true,
  );
  await startBuffer.fill("0");
  await endBuffer.fill("0");
  await form.locator("[data-source-remove]").first().click();
  await form.locator("[data-source-remove]").first().click();
  await form.locator('[data-action="sources-save"]').click();
  await page.waitForFunction(() => panel._activeActions === 0);
  assert.equal(
    await page.evaluate(
      () => fixture.devices.a.presence_sources.start_buffer_seconds,
    ),
    0,
  );
  assert.equal(
    await page.evaluate(
      () => fixture.devices.a.presence_sources.end_buffer_seconds,
    ),
    0,
  );
  assert.deepEqual(
    await page.evaluate(() => fixture.devices.a.presence_sources.sources),
    [],
  );
  await page.evaluate(async () => {
    fixture.devices.a.auto_learning = {
      profile: { periods: { human: 2, entity: 3, radar: 4 } },
      last: {
        state: "present",
        confidence: 0.65,
        basis: "adaptive-guided",
        unfamiliar_gates: ["g3_still"],
      },
    };
    panel._setSectionCollapsed("a", "auto", false);
    await panel._load();
  });
  assert.match(
    await card.locator(".reference-status").innerText(),
    /2 human, 3 entity and 4 inferred periods/,
  );
  assert.match(
    await card.locator(".reference-review").innerText(),
    /Unfamiliar readings at g3_still/,
  );
  assert.match(await card.innerText(), /Age-weighted room references/);
  assert.equal(await card.locator(".reference-stale").count(), 0);
  await card.locator(".reference-status").screenshot({
    path: path.join(screenshotDir, "adaptive-references-mobile.png"),
  });
  // P1: events only mark references stale; the panel says so and offers a refresh.
  await page.evaluate(async () => {
    fixture.devices.a.auto_learning.stale_since =
      Date.UTC(2026, 9, 9, 21) / 1000;
    window.requests = [];
    await panel._load();
  });
  assert.match(
    await card.locator(".reference-stale").innerText(),
    /References out of date since .*2026.*22:00.*\. They rebuild daily at the learning schedule time, even with overnight learning off, or now with Refresh references\./,
  );
  assert.equal(await card.locator(".reference-refreshing").count(), 0);
  await card.locator('[data-action="refresh-references"]').click();
  await page.waitForFunction(() => panel._activeActions === 0);
  assert.deepEqual(
    await page.evaluate(() =>
      requests
        .filter((message) => message.type.endsWith("/refresh_references"))
        .map((message) => message.device_id),
    ),
    ["a"],
  );
  assert.ok(
    await page.evaluate(() =>
      requests.some((message) => message.type.endsWith("/snapshot")),
    ),
    "the panel reloads after queueing a refresh",
  );
  // While the rebuild is queued or computing, the panel says so instead.
  await page.evaluate(async () => {
    fixture.devices.a.references_refreshing = true;
    await panel._load();
  });
  assert.equal(
    await card.locator(".reference-refreshing").innerText(),
    "Refreshing references…",
  );
  assert.equal(await card.locator(".reference-stale").count(), 0);
  assert.equal(
    await card.locator('[data-action="refresh-references"]').isDisabled(),
    true,
  );
  await page.evaluate(async () => {
    fixture = beforeSources;
    panel._hass.callWS = sourcesWS;
    panel._hass.states = beforeSourceStates;
    panel._sourceDrafts.clear();
    await panel._load();
  });
  await page.setViewportSize({ width: 1400, height: 1000 });
};
