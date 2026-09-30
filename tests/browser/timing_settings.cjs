const assert = require("node:assert/strict");
const path = require("node:path");

module.exports = async function testTimingSettings(page, screenshotDir) {
  await page.evaluate(async () => {
    window.beforeTimingPolicy = structuredClone(fixture);
    window.timingPolicyWS = panel._hass.callWS;
    fixture.timing_settings = {
      mode: "device",
      timeout: 1,
      on_delay: 0.5,
      off_delay: 1,
    };
    panel._hass.callWS = async (message) => {
      if (message.type.endsWith("/configure_timing")) {
        requests.push(message);
        if (window.failTimingSave)
          throw new Error("Timing settings could not be saved");
        fixture.timing_settings = structuredClone(message.settings);
        return message.settings;
      }
      return timingPolicyWS(message);
    };
    panel._timingOpen = true;
    await panel._load(true);
  });
  assert.equal(await page.locator(".panel-intro h1").count(), 1);
  assert.equal(await page.locator(".panel-intro .subtitle").count(), 1);
  assert.equal(
    await page.locator(".global-settings-card #learning-schedule").count(),
    1,
  );
  assert.equal(
    await page.locator(".global-settings-card #storage-controls").count(),
    1,
  );
  await page.locator(".global-settings-card > summary").click();
  const form = page.locator("#timing-controls");
  const mode = form.locator('[data-action="timing-mode"]');
  const on = form.locator('[data-timing="on_delay"]');
  assert.equal(await on.isDisabled(), true);
  await mode.selectOption("fallback");
  await on.fill("0.75");
  await form.locator('[data-timing="timeout"]').fill("4");
  await on.blur();
  await page.evaluate(() => panel._load(true));
  assert.equal(
    await on.inputValue(),
    "0.75",
    "polling preserves the timing draft",
  );
  await page.evaluate(() => {
    window.failTimingSave = true;
  });
  await form.locator('[data-action="timing-save"]').click();
  await page.waitForFunction(() => panel._activeActions === 0);
  assert.match(
    await page.locator("#error").innerText(),
    /Timing settings could not be saved/,
  );
  assert.equal(await on.inputValue(), "0.75");
  await page.evaluate(() => {
    window.failTimingSave = false;
  });
  await form.locator('[data-action="timing-save"]').click();
  await page.waitForFunction(() => panel._activeActions === 0);
  assert.deepEqual(await page.evaluate(() => fixture.timing_settings), {
    mode: "fallback",
    timeout: 4,
    on_delay: 0.75,
    off_delay: 1,
  });
  await mode.selectOption("disabled");
  assert.equal(await on.isDisabled(), true);
  await form.locator('[data-action="timing-save"]').click();
  await page.waitForFunction(() => panel._activeActions === 0);
  assert.equal(
    await page.evaluate(() => fixture.timing_settings.mode),
    "disabled",
  );

  await page.evaluate(() => {
    fixture.devices.a.nightly_learning = {
      status: "uncertain",
      started_at: 2000,
      finished_at: 2010,
      assessment: {
        presence_recall: 1,
        presence_recall_lower: 0.999,
        false_positive_percent: 0.25,
      },
      timing: {
        scope: "radar",
        configuration: { timeout: 1, on_delay: null, off_delay: null },
      },
    };
    return panel._load(true);
  });
  const pill = page.locator('[data-device-id="a"] .nightly-marker');
  assert.match(
    await pill.innerText(),
    /Completed.*Recall 99.900–100.000%.*false-active 0.25%/,
  );
  assert.doesNotMatch(await pill.innerText(), /Failed|Timing uncertain/);
  assert.match(await pill.getAttribute("class"), /caution/);
  assert.doesNotMatch(await pill.getAttribute("class"), /warn/);
  assert.match(await pill.getAttribute("title"), /Sampling uncertainty/);
  const amber = await pill.evaluate(
    (element) => getComputedStyle(element).color,
  );
  await page.evaluate(() => {
    fixture.devices.a.nightly_learning.status = "error";
    fixture.devices.a.nightly_learning.error = "Device offline";
    return panel._load(true);
  });
  assert.match(await pill.innerText(), /Failed/);
  assert.notEqual(
    await pill.evaluate((element) => getComputedStyle(element).color),
    amber,
  );
  await page.evaluate(() => {
    fixture.devices.a.nightly_learning.status = "unsafe";
    delete fixture.devices.a.nightly_learning.error;
    return panel._load(true);
  });
  assert.match(await pill.getAttribute("class"), /warn/);

  const notes = await page.evaluate(() => {
    const configuration = {
      mode: "fallback",
      timeout: 4,
      on_delay: 0.75,
      off_delay: 1,
      sources: {
        timeout: "device",
        on_delay: "fallback",
        off_delay: "fallback",
      },
    };
    const learning = { timing: { scope: "reported_presence", configuration } };
    return {
      fallback: panel._timingPolicyNote(learning.timing),
      disabled: panel._timingPolicyNote({
        configuration: { mode: "disabled" },
      }),
      changed: panel._timingChanged(learning, {
        ...configuration,
        mode: "disabled",
      }),
      caution: panel._timingNeedsReview(learning),
    };
  });
  assert.match(
    notes.fallback,
    /Global defaults assumed for on delay, off delay/,
  );
  assert.match(notes.disabled, /raw threshold activity/);
  assert.equal(notes.changed, true);
  assert.equal(notes.caution, true);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator(".global-settings-card").screenshot({
    path: path.join(screenshotDir, "timing-settings-mobile.png"),
  });
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
    true,
  );
  const geometry = await page.evaluate(() => {
    const header = panel.shadowRoot
      .querySelector(".panel-intro")
      .getBoundingClientRect();
    const settings = panel.shadowRoot
      .querySelector(".global-settings-card")
      .getBoundingClientRect();
    return {
      headerBottom: header.bottom,
      settingsTop: settings.top,
      left: settings.left,
      right: settings.right,
    };
  });
  assert.ok(geometry.settingsTop > geometry.headerBottom);
  assert.ok(geometry.left >= 0 && geometry.right <= 390);
  await page.evaluate(async () => {
    fixture = beforeTimingPolicy;
    panel._hass.callWS = timingPolicyWS;
    panel._timingDraft = null;
    panel._timingOpen = false;
    await panel._load(true);
  });
  await page.setViewportSize({ width: 1400, height: 1000 });
};
