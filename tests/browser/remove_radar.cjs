const assert = require("node:assert/strict");

// A radar no longer in Home Assistant is listed with a way to delete it; deleting asks first,
// sends remove_radar and the list goes once nothing is left in it.
module.exports = async function testRemoveRadar(page) {
  await page.evaluate(async () => {
    window.removeBefore = structuredClone(fixture);
    window.removeWS = panel._hass.callWS;
    window.removeConfirm = window.confirm;
    window.removeAnswer = false;
    window.confirm = () => window.removeAnswer;
    fixture.departed_radars = [
      { device_id: "old", name: "Old hallway radar", area_id: null },
    ];
    panel._hass.callWS = async (message) => {
      if (message.type.endsWith("remove_radar")) {
        window.requests.push(message);
        fixture.departed_radars = fixture.departed_radars.filter(
          (item) => item.device_id !== message.device_id,
        );
        return { ok: true };
      }
      return removeWS(message);
    };
    await panel._load();
  });
  const section = page.locator("[data-departed-radars]");
  assert.equal(await section.count(), 1);
  await section.locator("summary").click();
  const row = section.locator('.excluded-radar[data-device-id="old"]');
  assert.equal(await row.locator("span").innerText(), "Old hallway radar");
  assert.equal(await row.locator('[data-action="include"]').count(), 0);

  const removals = () =>
    page.evaluate(
      () => requests.filter((m) => m.type.endsWith("remove_radar")).length,
    );
  const before = await removals();
  await row.locator('[data-action="remove-radar"]').click();
  assert.equal(await removals(), before, "declining the prompt sends nothing");

  await page.evaluate(() => (window.removeAnswer = true));
  await row.locator('[data-action="remove-radar"]').click();
  await page.waitForFunction(
    () => panel.shadowRoot.querySelector("[data-departed-radars]") === null,
  );
  const sent = await page.evaluate(() =>
    requests.filter((m) => m.type.endsWith("remove_radar")),
  );
  assert.equal(sent.at(-1).device_id, "old");

  await page.evaluate(async () => {
    panel._hass.callWS = removeWS;
    window.confirm = removeConfirm;
    window.fixture = removeBefore;
    await panel._load();
  });
};
