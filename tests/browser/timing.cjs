const assert = require("node:assert/strict");
const path = require("node:path");

module.exports = async function testTiming(page, screenshotDir) {
  await page.evaluate(() => {
    window.beforeTiming = structuredClone(fixture.devices.a);
    window.beforeTimingDetails = Object.fromEntries(
      [
        ...panel.shadowRoot.querySelectorAll(
          '[data-device-id="a"] details[data-detail]',
        ),
      ].map((node) => [node.dataset.detail, node.open]),
    );
    panel._setSectionCollapsed("a", "details", false);
    const learning = fixture.devices.a.last_learning;
    learning.status = "ok";
    learning.feasibility = { status: "not_assessed" };
    learning.timing = {
      active: true,
      scope: "reported_presence",
      sample_interval_seconds: 6,
      configuration: { timeout: 15, on_delay: 0.5, off_delay: 1 },
    };
    learning.raw_training = {
      false_negatives: 12,
      present_samples: 1000,
      false_positives: 3,
      not_present_samples: 1000,
    };
    learning.training.timing_warmup_samples = 7;
    learning.review = {
      period_count: 1,
      periods: [
        {
          start: 2000,
          end: 2018,
          state: "not_present",
          samples: 3,
          observed_span_seconds: 12,
        },
      ],
    };
    fixture.devices.a.timing_configuration = {
      timeout: 15,
      on_delay: 0.5,
      off_delay: 1,
    };
    return panel._load();
  });
  const card = page.locator('[data-device-id="a"]');
  const report = card.locator(".timing-report");
  await report.locator("summary").click();
  assert.match(
    await report.innerText(),
    /Radar timeout: 15s.*On delay: 0.5s.*Off delay: 1s/,
  );
  assert.match(await report.innerText(), /7 initial observations/);
  assert.match(await report.innerText(), /Before hold and filters: 12 missed/);
  assert.match(await report.innerText(), /cannot establish sub-second/);
  await card.locator(".evidence-review > summary").click();
  assert.match(
    await card.locator(".evidence-review").innerText(),
    /observations span 12s; actual duration unknown/,
  );
  await card
    .locator('.subsection[data-section="details"]')
    .screenshot({ path: path.join(screenshotDir, "timing-report.png") });
  await page.evaluate(() => {
    fixture.devices.a.timing_configuration.timeout = 5;
    return panel._load();
  });
  assert.match(await report.innerText(), /Device timing has changed/);
  assert.match(
    await card.locator('[data-action="apply"]').getAttribute("class"),
    /apply-caution/,
  );
  assert.match(
    await card.locator(".learning-report").getAttribute("class"),
    /outcome-caution/,
  );
  assert.equal(await card.locator('[data-action="apply"]').isDisabled(), false);
  await page.evaluate(() => {
    fixture.devices.a.last_learning.timing = {
      active: true,
      scope: "radar",
      configuration: { timeout: 15, on_delay: null, off_delay: null },
    };
    delete fixture.devices.a.timing_configuration;
    return panel._load();
  });
  assert.match(await report.innerText(), /ESPHome filters are unknown/);
  assert.equal(await card.locator('[data-action="apply"]').isDisabled(), false);
  await page.evaluate(() => {
    fixture.devices.a.last_learning.timing = {
      active: false,
      scope: "raw",
      configuration: { timeout: null },
    };
    return panel._load();
  });
  assert.match(
    await report.innerText(),
    /Timeout is unavailable.*raw threshold crossings/,
  );
  assert.equal(await card.locator('[data-action="apply"]').isDisabled(), false);
  await page.evaluate(() => {
    delete fixture.devices.a.last_learning.timing;
    return panel._load();
  });
  assert.match(
    await report.innerText(),
    /saved result has no timing assessment/,
  );
  await page.evaluate(() => {
    for (const node of panel.shadowRoot.querySelectorAll(
      '[data-device-id="a"] details[data-detail]',
    ))
      node.open = window.beforeTimingDetails[node.dataset.detail] || false;
    fixture.devices.a = window.beforeTiming;
    return panel._load();
  });
};
