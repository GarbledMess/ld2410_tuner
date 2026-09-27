const assert = require("node:assert/strict");
const path = require("node:path");

module.exports = async function testJobs(page, screenshotDir) {
  await page.evaluate(() => {
    window.jobsOriginalPanel = panel;
    window.jobsOriginalWS = panel._hass.callWS;
    window.jobsOriginalDevice = structuredClone(fixture.devices.a);
    window.jobStarts = 0;
    panel._hass.callWS = (message) => {
      if (message.type.endsWith("/start_learning")) {
        requests.push(message);
        jobStarts++;
        fixture.devices.a.learning_job = {
          id: "background-job",
          status: "running",
          stage: "preparing",
          sources: ["user"],
          started_at: Date.now() / 1000 - 65,
        };
        return Promise.resolve(structuredClone(fixture.devices.a.learning_job));
      }
      return jobsOriginalWS(message);
    };
    panel._collapsed.delete("a");
    panel._setSectionCollapsed("a", "details", false);
    panel._draw();
  });
  const card = page.locator('[data-device-id="a"]');
  const applies = await page.evaluate(
    () => requests.filter((m) => m.type.endsWith("/apply")).length,
  );
  await card.locator('[data-action="learn"]').click();
  await page.waitForFunction(
    () => jobStarts === 1 && panel._activeActions === 0,
  );
  assert.match(
    await card.locator(".learning-job-status").innerText(),
    /Checking radar settings/,
  );
  assert.match(await card.locator(".learning-job-status").innerText(), /1m/);
  assert.equal(await card.locator('[data-action="learn"]').isDisabled(), true);
  assert.equal(await card.locator('[data-action="state"]').isDisabled(), false);
  assert.equal(await card.locator("progress").getAttribute("value"), null);

  // Destroy the page component, then create a new one using only server snapshots.
  await page.evaluate(() => {
    const hass = panel._hass;
    panel.remove();
    window.panel = document.createElement("ld2410-tuner-panel");
    document.body.append(panel);
    panel.hass = hass;
  });
  await card.locator(".learning-job-status progress").waitFor();
  assert.equal(
    await card
      .locator(".body")
      .evaluate((node) => node.classList.contains("collapsed")),
    true,
  );
  assert.equal(await card.locator(".learning-job-status").isVisible(), true);
  assert.equal(await page.evaluate(() => jobStarts), 1);
  assert.equal(await card.locator('[data-action="learn"]').isDisabled(), true);
  await card.locator('[data-action="toggle"]').click();
  await card.locator('[data-action="timeout-hours"]').focus();
  await page.evaluate(async () => {
    window.jobFocusedField = panel.shadowRoot.activeElement;
    fixture.devices.a.learning_job.stage = "fitting";
    await panel._load();
  });
  assert.match(
    await card.locator(".learning-job-status").innerText(),
    /Fitting and validating/,
  );
  assert.equal(
    await page.evaluate(
      () => panel.shadowRoot.activeElement === jobFocusedField,
    ),
    true,
  );
  await page.evaluate(() => panel.shadowRoot.activeElement.blur());
  await page.setViewportSize({ width: 390, height: 844 });
  await card
    .locator(".learning-job-status")
    .screenshot({ path: path.join(screenshotDir, "learning-job-mobile.png") });
  const bounds = await card.locator(".learning-job-status").boundingBox();
  assert.ok(bounds && bounds.x >= 0 && bounds.x + bounds.width <= 390);

  // The fit finishes while detached; reconnection must fetch it immediately.
  await page.evaluate(() => {
    panel.remove();
    const device = fixture.devices.a;
    const result = structuredClone(
      device.learning_results?.user || device.last_learning,
    );
    result.id = "background-result";
    device.learning_results = {
      ...(device.learning_results || {}),
      user: result,
    };
    device.last_learning = result;
    Object.assign(device.learning_job, {
      status: "completed",
      finished_at: Date.now() / 1000,
      results: { user: result.id },
    });
    document.body.append(panel);
  });
  await card.locator('[data-action="review-learning-job"]').waitFor();
  assert.match(
    await card.locator(".learning-job-status").innerText(),
    /result saved/,
  );
  assert.equal(await card.locator("progress").count(), 0);
  assert.equal(await card.locator('[data-action="learn"]').isDisabled(), false);
  await card.locator('[data-action="review-learning-job"]').click();
  assert.equal(
    await card.locator('[data-action="learning-result"]').inputValue(),
    "user",
  );
  assert.equal(await card.locator('[data-action="apply"]').isVisible(), true);
  assert.equal(
    await page.evaluate(
      () => panel._learningView("a", panel._data.devices.a).last_learning.id,
    ),
    "background-result",
  );
  assert.equal(
    await page.evaluate(
      () => requests.filter((m) => m.type.endsWith("/apply")).length,
    ),
    applies,
  );

  // Failure and restart reports remain readable and do not keep Learn disabled.
  await page.evaluate(async () => {
    fixture.devices.a.learning_job.status = "error";
    fixture.devices.a.learning_job.error = "Radar unavailable <img src=x>";
    await panel._load();
  });
  assert.match(
    await card.locator(".learning-job-status").innerText(),
    /Radar unavailable <img src=x>/,
  );
  assert.equal(await card.locator(".learning-job-status img").count(), 0);
  assert.equal(await card.locator('[data-action="learn"]').isDisabled(), false);
  await page.evaluate(async () => {
    fixture.devices.a.learning_job.status = "interrupted";
    fixture.devices.a.learning_job.error =
      "Home Assistant restarted during learning";
    await panel._load();
  });
  assert.match(
    await card.locator(".learning-job-status").innerText(),
    /Learning was interrupted/,
  );
  assert.match(
    await card.locator(".learning-job-status").innerText(),
    /Home Assistant restarted/,
  );
  assert.equal(await page.evaluate(() => jobStarts), 1);
  await page.evaluate(async () => {
    panel.remove();
    fixture.devices.a = jobsOriginalDevice;
    window.panel = jobsOriginalPanel;
    panel._hass.callWS = jobsOriginalWS;
    document.body.append(panel);
    await panel._load(true);
    clearInterval(panel._pollTimer);
    panel._pollTimer = null;
  });
  await page.setViewportSize({ width: 1400, height: 1000 });
};
