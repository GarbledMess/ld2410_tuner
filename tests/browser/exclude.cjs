const assert = require("node:assert/strict");

// Excluding a radar tucks its card away into "Excluded radars"; "Include again" brings it back.
module.exports = async function testExclude(page) {
  await page.evaluate(async () => {
    window.excludeBefore = structuredClone(fixture);
    window.excludeWS = panel._hass.callWS;
    window.excludeConfirm = window.confirm;
    window.confirm = () => true;
    panel._hass.callWS = async (message) => {
      if (message.type.endsWith("set_excluded")) {
        window.requests.push(message);
        const id = message.device_id;
        if (message.excluded) {
          const device = fixture.devices[id];
          delete fixture.devices[id];
          fixture.excluded_radars = [
            ...(fixture.excluded_radars || []),
            { device_id: id, name: device.name, area_id: null, since: 0 },
          ];
          window.excludedDevice = device;
        } else {
          fixture.excluded_radars = fixture.excluded_radars.filter(
            (item) => item.device_id !== id,
          );
          fixture.devices[id] = window.excludedDevice;
        }
        return { excluded: message.excluded };
      }
      return excludeWS(message);
    };
    await panel._load();
  });
  const card = page.locator('#layout .card[data-device-id="c"]').first();
  assert.equal(await card.count(), 1);
  assert.equal(
    await page.locator("[data-excluded-radars]").count(),
    0,
    "no section while nothing is excluded",
  );

  await card.locator('[data-action="exclude"]').click();
  await page.waitForFunction(
    () => panel.shadowRoot.querySelector("[data-excluded-radars]") !== null,
  );
  const sent = await page.evaluate(() =>
    requests.filter((m) => m.type.endsWith("set_excluded")),
  );
  assert.deepEqual(
    sent.map((m) => [m.device_id, m.excluded]),
    [["c", true]],
  );
  assert.equal(
    await page.locator('#layout .card[data-device-id="c"]').count(),
    0,
  );
  const section = page.locator("[data-excluded-radars]");
  assert.equal(await section.count(), 1, "one section, not one per redraw");
  await page.evaluate(() => panel._load());
  assert.equal(await page.locator("[data-excluded-radars]").count(), 1);
  await section.locator("summary").click();
  const row = section.locator('.excluded-radar[data-device-id="c"]');
  assert.equal(await row.locator("span").innerText(), "Room c");

  await row.locator('[data-action="include"]').click();
  await page.waitForFunction(
    () => panel.shadowRoot.querySelector("[data-excluded-radars]") === null,
  );
  assert.equal(
    await page.locator('#layout .card[data-device-id="c"]').count(),
    1,
  );

  await page.evaluate(async () => {
    panel._hass.callWS = excludeWS;
    window.confirm = excludeConfirm;
    window.fixture = excludeBefore;
    await panel._load();
  });
};
