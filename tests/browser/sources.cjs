const assert = require("node:assert/strict");
const path = require("node:path");

module.exports = async function testSources(page, screenshotDir) {
  await page.evaluate(async () => {
    window.beforeSources = structuredClone(fixture);
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
  await form
    .locator("[data-source-entity]")
    .fill("binary_sensor.camera_person");
  await form.locator('[data-action="source-add"]').click();
  await form.locator("[data-source-kind]").nth(1).selectOption("bermuda");
  await form.locator("[data-source-entity]").nth(1).fill("sensor.phone_area");
  await form.locator("[data-source-area]").fill("bedroom");
  await page.evaluate(() => panel.shadowRoot.activeElement.blur());
  await page.evaluate(() => panel._load());
  assert.equal(
    await form.locator("[data-source-entity]").nth(1).inputValue(),
    "sensor.phone_area",
  );
  assert.equal(
    await form.locator("[data-source-area]").inputValue(),
    "bedroom",
  );
  assert.equal(await form.locator("[data-source-negative]").isChecked(), false);
  await form.locator('[data-action="sources-save"]').click();
  await page.waitForFunction(() => panel._activeActions === 0);
  assert.deepEqual(
    await page.evaluate(() => fixture.devices.a.presence_sources),
    {
      sources: [
        { entity_id: "binary_sensor.camera_person", kind: "boolean", area: "" },
        { entity_id: "sensor.phone_area", kind: "bermuda", area: "bedroom" },
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
    fixture = beforeSources;
    panel._hass.callWS = sourcesWS;
    panel._hass.states = beforeSourceStates;
    panel._sourceDrafts.clear();
    await panel._load();
  });
  await page.setViewportSize({ width: 1400, height: 1000 });
};
