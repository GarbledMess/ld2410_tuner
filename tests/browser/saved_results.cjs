const assert = require("node:assert/strict");

module.exports = async function testSavedResults(page, screenshotDir) {
  await page.evaluate(() => {
    window.beforeSavedResults = structuredClone(fixture.devices.a);
    window.beforeScheduleWS = panel._hass.callWS;
    fixture.learning_schedule = {
      enabled: false,
      time: "03:00",
      timezone: "Europe/London",
    };
    const original = fixture.devices.a.last_learning;
    fixture.devices.a.learning_results = Object.fromEntries(
      ["user", "previous", "current", "automatic"].map((slot, index) => {
        const result = structuredClone(original);
        result.id = `${slot}-reviewed`;
        result.created_at = Date.parse("2026-09-26T03:00:00Z") / 1000;
        result.proposals.g0_move.threshold = 20 + index;
        result.status = slot === "automatic" ? "unsafe" : "ok";
        return [slot, result];
      }),
    );
    fixture.devices.a.nightly_learning = {
      status: "unsafe",
      started_at: Date.now() / 1000,
    };
    panel._hass.callWS = async (message) => {
      if (message.type.endsWith("/configure_learning_schedule")) {
        requests.push(message);
        fixture.learning_schedule = {
          ...fixture.learning_schedule,
          enabled: message.enabled,
          time: message.at,
        };
        return fixture.learning_schedule;
      }
      if (message.type.endsWith("/apply")) {
        requests.push(message);
        return { applied: {}, skipped: {} };
      }
      return window.beforeScheduleWS(message);
    };
    panel._setSectionCollapsed("a", "details", false);
    panel._collapsed.delete("a");
    return panel._load();
  });
  const card = page.locator('[data-device-id="a"]');
  const picker = card.locator('[data-action="learning-result"]');
  const apply = card.locator('[data-action="apply"]');
  assert.match(
    await card.locator(".nightly-marker").innerText(),
    /Targets not met/,
  );
  assert.equal(await picker.locator("option").count(), 4);
  await page.evaluate(() => {
    fixture.devices.a.nightly_learning.status = "running";
    fixture.learning_schedule.running = true;
    return panel._load();
  });
  assert.equal(await card.locator(".nightly-marker.is-busy").count(), 1);
  assert.equal(await page.locator("#learning-schedule .is-busy").count(), 1);
  await page.evaluate(() => {
    fixture.devices.a.nightly_learning.status = "unsafe";
    delete fixture.learning_schedule.running;
    return panel._load();
  });
  for (const slot of ["automatic", "previous", "current", "user"]) {
    await picker.selectOption(slot);
    const index = ["user", "previous", "current", "automatic"].indexOf(slot);
    assert.match(
      await card.locator(".learn-status").innerText(),
      new RegExp(`threshold ${20 + index}`),
    );
    page.once("dialog", (dialog) => dialog.accept());
    await apply.click();
    await page.waitForFunction(() => panel._activeActions === 0);
    const message = await page.evaluate(() =>
      requests.filter((item) => item.type.endsWith("/apply")).at(-1),
    );
    assert.equal(message.slot, slot);
    assert.equal(message.result_id, `${slot}-reviewed`);
    assert.deepEqual(message.expected, { g0_move: 20 });
    await page.evaluate(() => panel._load());
    assert.equal(
      await picker.inputValue(),
      slot,
      "polling preserves selection",
    );
  }
  // A newer overnight result arriving during an edit cannot silently replace the reviewed Apply payload.
  await picker.selectOption("automatic");
  await card.locator('[data-action="history-start"]').focus();
  await page.evaluate(() => {
    fixture.devices.a.learning_results.automatic.id = "automatic-newer";
    return panel._load();
  });
  page.once("dialog", (dialog) => dialog.accept());
  await apply.click();
  await page.waitForFunction(() => panel._activeActions === 0);
  assert.equal(
    await page.evaluate(
      () =>
        requests.filter((item) => item.type.endsWith("/apply")).at(-1)
          .result_id,
    ),
    "automatic-reviewed",
  );

  await page.locator('[data-action="nightly-enabled"]').check();
  await page.locator('[data-action="nightly-time"]').fill("04:15");
  await page.locator('[data-action="nightly-time"]').blur();
  await page.evaluate(() => panel._load());
  assert.equal(
    await page.locator('[data-action="nightly-time"]').inputValue(),
    "04:15",
    "unsaved schedule survives polling",
  );
  await page.locator('[data-action="nightly-save"]').click();
  await page.waitForFunction(() => panel._activeActions === 0);
  assert.deepEqual(await page.evaluate(() => fixture.learning_schedule), {
    enabled: true,
    time: "04:15",
    timezone: "Europe/London",
  });
  await page.setViewportSize({ width: 1400, height: 1000 });
  await page
    .locator("#learning-schedule")
    .screenshot({ path: `${screenshotDir}/overnight-schedule.png` });
  await card
    .locator('.subsection[data-section="details"]')
    .screenshot({ path: `${screenshotDir}/saved-results-desktop.png` });
  await page.setViewportSize({ width: 390, height: 844 });
  await card
    .locator('.subsection[data-section="details"]')
    .screenshot({ path: `${screenshotDir}/saved-results-mobile.png` });
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
    true,
  );
  await page.setViewportSize({ width: 1400, height: 1000 });
  await page.evaluate(() => {
    fixture.devices.a = window.beforeSavedResults;
    panel._learningSelection.delete("a");
    panel._hass.callWS = window.beforeScheduleWS;
    return panel._load();
  });
};
