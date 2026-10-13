const assert = require("node:assert/strict");

module.exports = async function testAutomaticWeight(page) {
  await page.evaluate(async () => {
    window.beforeWeight = {
      schedule: structuredClone(fixture.learning_schedule),
      device: structuredClone(fixture.devices.a),
      ws: panel._hass.callWS,
      open: panel.shadowRoot.querySelector(".global-settings-card").open,
    };
    fixture.learning_schedule = {
      enabled: false,
      time: "03:00",
      timezone: "Europe/London",
      automatic_weight: 0,
      default_automatic_weight: 0,
    };
    panel._hass.callWS = async (message) => {
      if (message.type.endsWith("/configure_automatic_weight")) {
        requests.push(message);
        fixture.learning_schedule.automatic_weight = message.weight;
        return {
          automatic_weight: message.weight,
          default_automatic_weight: 0,
        };
      }
      if (message.type.endsWith("/configure_learning_schedule")) {
        requests.push(message);
        return fixture.learning_schedule;
      }
      return window.beforeWeight.ws(message);
    };
    panel.shadowRoot.querySelector(".global-settings-card").open = true;
    panel._setSectionCollapsed("a", "details", false);
    panel._collapsed.delete("a");
    window.weightStart = requests.length;
    await panel._load(true);
  });
  const schedule = page.locator("#learning-schedule");
  const weight = schedule.locator('[data-action="automatic-weight"]');
  const save = schedule.locator('[data-action="nightly-save"]');
  const weightRequests = () =>
    page.evaluate(() =>
      requests
        .slice(window.weightStart)
        .filter((message) =>
          message.type.endsWith("/configure_automatic_weight"),
        ),
    );
  assert.match(await schedule.innerText(), /Automatic evidence weight/);
  const help = await schedule.innerText();
  assert.match(help, /0 = automatic evidence only breaks ties/);
  assert.match(
    help,
    /1 = automatic evidence counts up to a quarter of the human evidence/,
  );
  assert.match(
    help,
    /Automatic Apply still requires an improvement on human labels/,
  );
  assert.match(help, /not yet validated on real data/);
  assert.equal(await weight.getAttribute("step"), "any");
  assert.equal(await weight.inputValue(), "0");
  assert.equal(await weight.getAttribute("min"), "0");
  assert.equal(await weight.getAttribute("max"), "1");

  await save.click();
  await page.waitForFunction(() => panel._activeActions === 0);
  assert.deepEqual(await weightRequests(), [], "unchanged weight is not sent");

  await weight.fill("1.5");
  await save.click();
  await page.waitForFunction(() => panel._activeActions === 0);
  assert.deepEqual(
    await weightRequests(),
    [],
    "out-of-range weight is refused",
  );

  await weight.fill("0.3");
  await weight.blur();
  await page.evaluate(() => panel._load());
  assert.equal(
    await weight.inputValue(),
    "0.3",
    "unsaved weight survives polling",
  );
  await save.click();
  await page.waitForFunction(() => panel._activeActions === 0);
  assert.deepEqual(
    (await weightRequests()).map((message) => message.weight),
    [0.3],
  );
  assert.equal(await weight.inputValue(), "0.3");

  const details = page.locator(
    '[data-device-id="a"] .subsection[data-section="details"]',
  );
  const notice = /automatic evidence weight has changed/;
  assert.match(await details.innerText(), notice, "older result is stale");
  await page.evaluate(() => {
    for (const result of Object.values(
      fixture.devices.a.learning_results || {},
    ))
      result.automatic_weight = 0.3;
    if (fixture.devices.a.last_learning)
      fixture.devices.a.last_learning.automatic_weight = 0.3;
    return panel._load();
  });
  assert.doesNotMatch(await details.innerText(), notice);

  await page.evaluate(() => {
    fixture.learning_schedule = window.beforeWeight.schedule;
    fixture.devices.a = window.beforeWeight.device;
    panel._hass.callWS = window.beforeWeight.ws;
    panel._scheduleDraft = null;
    panel.shadowRoot.querySelector(".global-settings-card").open =
      window.beforeWeight.open;
    return panel._load(true);
  });
};
