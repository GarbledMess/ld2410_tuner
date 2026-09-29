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
    const learning = fixture.devices.a.last_learning;
    learning.status = "uncertain";
    learning.training.onset_uncertainty = {
      samples: 4,
      misses_if_earliest_onset: 4,
      misses_if_latest_onset: 8,
    };
    fixture.devices.a.nightly_learning = {
      status: "uncertain",
      started_at: 2000,
      finished_at: 2010,
    };
    return panel._load();
  });
  assert.match(
    await report.innerText(),
    /4 presence observations have unresolved transition timing/,
  );
  assert.match(await card.locator(".sample-outcomes").innerText(), /4–8/);
  assert.match(
    await card.locator('[data-action="apply"]').getAttribute("class"),
    /apply-caution/,
  );
  assert.equal(await card.locator('[data-action="apply"]').isDisabled(), false);
  assert.match(
    await card.locator(".nightly-marker").innerText(),
    /Completed · review result/,
  );
  assert.match(
    await card.locator(".nightly-report").innerText(),
    /presence accuracy is not confirmed/,
  );
  await page.evaluate(() => {
    fixture.devices.a.last_learning.status = "ok";
    delete fixture.devices.a.last_learning.training.onset_uncertainty;
    delete fixture.devices.a.nightly_learning;
    return panel._load();
  });
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
    /Timeout is unavailable.*raw threshold activity/,
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
    const learning = fixture.devices.a.last_learning;
    learning.status = "uncertain";
    learning.timing = {
      active: true,
      scope: "reported_presence",
      sample_interval_seconds: 6,
      configuration: { timeout: 1, on_delay: 0.5, off_delay: 1 },
    };
    learning.training.duration = {
      presence_recall: 0.9994,
      presence_recall_lower: 0.9986,
      error_cost: 2.75,
      missed_time_cost: 5,
      false_positive_score: -2.05,
      false_positive_percent: 2.05,
      false_positive_seconds: 440,
      empty_seconds: 21418,
      present_seconds: 30034,
      missed_seconds: 20,
      missed_seconds_upper: 44,
      longest_missed_seconds: 5,
      missed_presence_episodes: 0,
    };
    learning.review = {
      influence: [
        {
          start: Date.parse("2026-09-23T16:00:00Z") / 1000,
          end: Date.parse("2026-09-23T18:00:00Z") / 1000,
          state: "not_present",
          error_share_percent: 73,
          error_seconds: 321,
          score_without_period: -0.84,
        },
      ],
    };
    fixture.devices.a.nightly_learning = {
      status: "tradeoff",
      started_at: 2000,
      finished_at: 2010,
    };
    return panel._load();
  });
  assert.match(
    await card.locator(".learning-metrics").innerText(),
    /99.860–99.940%/,
  );
  assert.match(await card.locator(".learning-metrics").innerText(), /-2.050/);
  assert.match(
    await card.locator(".learning-report").innerText(),
    /Search error cost: 2.750/,
  );
  assert.match(await card.locator(".learning-report").innerText(), /costs 5×/);
  assert.match(await card.locator(".evidence-influence").innerText(), /73.0%/);
  assert.match(
    await card.locator(".evidence-influence").innerText(),
    /same thresholds.*-0.840/,
  );
  assert.match(
    await card.locator(".nightly-report").innerText(),
    /Completed and scored/,
  );
  assert.equal(await card.locator('[data-action="apply"]').isDisabled(), false);
  await page.evaluate(() => {
    const learning = fixture.devices.a.last_learning;
    learning.status = "tradeoff";
    learning.training.duration.presence_recall_lower =
      learning.training.duration.presence_recall;
    learning.training.duration.missed_seconds_upper =
      learning.training.duration.missed_seconds;
    return panel._load();
  });
  await card
    .locator('.subsection[data-section="details"]')
    .screenshot({ path: path.join(screenshotDir, "duration-influence.png") });
  await card
    .locator('.evidence-influence [data-action="review-period"]')
    .click();
  await page.waitForFunction(() => !panel._chartState.get("a").loading);
  assert.equal(
    await card.locator('[data-action="history-start"]').inputValue(),
    "2026-09-23T17:00",
  );
  assert.equal(
    await card.locator('[data-action="history-end"]').inputValue(),
    "2026-09-23T19:00",
  );
  await page.evaluate(() => {
    window.recoveryOperation = panel._beginAction(
      panel.shadowRoot.querySelector(
        '[data-device-id="a"] [data-action="learn"]',
      ),
    );
    fixture.devices.a.configuration_recovery = {
      status: "running",
      stage: "bluetooth_cycle",
      steps: ["query_parameters", "bluetooth_cycle"],
    };
    return panel._load();
  });
  assert.match(
    await card.locator(".recovery-report").innerText(),
    /Cycling Bluetooth/,
  );
  assert.match(
    await card.locator(".recovery-report").getAttribute("class"),
    /is-busy/,
  );
  assert.equal(await card.locator('[data-action="learn"]').isDisabled(), true);
  await page.evaluate(() => {
    fixture.devices.a.configuration_recovery.status = "failed";
    fixture.devices.a.configuration_recovery.error = "No parameter response";
    fixture.devices.a.configuration_recovery.restore_bluetooth = {
      state: "off",
    };
    return panel._load();
  });
  assert.match(
    await card.locator(".recovery-report").innerText(),
    /No parameter response/,
  );
  assert.match(
    await card.locator(".recovery-report").innerText(),
    /still needs restoration/,
  );
  await page.evaluate(() => panel._endAction(window.recoveryOperation));
  await page.evaluate(() => {
    const l = fixture.devices.a.last_learning;
    l.status = "uncertain";
    l.training.present_samples = 1000;
    l.training.not_present_samples = 1000;
    l.training.false_negatives = 4;
    l.training.false_positives = 12;
    l.training.basis = "sampled_timing_estimate";
    l.training.onset_uncertainty = {
      samples: 4,
      misses_if_earliest_onset: 4,
      misses_if_latest_onset: 8,
    };
    fixture.devices.a.timing_configuration = structuredClone(
      l.timing.configuration,
    );
    panel._setSectionCollapsed("a", "details", false);
    return panel._load();
  });
  const outcomes = card.locator(".sample-outcomes");
  assert.match(
    await outcomes.innerText(),
    /Presence detected \(hits\).*992–996 \/ 1000/s,
  );
  assert.match(await outcomes.innerText(), /Presence missed.*4–8 \/ 1000/s);
  assert.match(await outcomes.innerText(), /Correctly empty.*988 \/ 1000/s);
  assert.match(await outcomes.innerText(), /False presence.*12 \/ 1000/s);
  assert.equal(
    await card.locator(".threshold-line.learned-caution").count(),
    1,
  );
  assert.equal(await card.locator(".threshold-line.learned-unsafe").count(), 0);
  assert.match(
    await card.locator(".learn-status").innerText(),
    /Timing uncertain/,
  );
  await card
    .locator('.subsection[data-section="details"]')
    .screenshot({ path: path.join(screenshotDir, "sample-outcomes.png") });
  const viewport = page.viewportSize();
  await page.setViewportSize({ width: 375, height: 812 });
  assert.equal(await outcomes.locator("div").count(), 4);
  for (const item of await outcomes.locator("div").all()) {
    const bounds = await item.boundingBox();
    assert.ok(bounds && bounds.x >= 0 && bounds.x + bounds.width <= 375);
  }
  await outcomes.screenshot({
    path: path.join(screenshotDir, "sample-outcomes-mobile.png"),
  });
  await page.setViewportSize(viewport);

  await page.evaluate(() => {
    fixture.devices.a.timing_configuration.timeout = 20;
    return panel._load();
  });
  assert.match(
    await card.locator('[data-action="apply"]').innerText(),
    /Device timing changed/,
  );
  assert.match(
    await card.locator(".learn-status").innerText(),
    /Device timing changed/,
  );
  page.once("dialog", async (dialog) => {
    assert.match(dialog.message(), /Device timing changed/);
    await dialog.dismiss();
  });
  await card.locator('[data-action="apply"]').click();
  await page.waitForFunction(() => panel._activeActions === 0);
  await page.evaluate(() => {
    const l = fixture.devices.a.last_learning;
    l.method = "human_priority_v9";
    l.status = "tradeoff";
    return panel._load();
  });
  assert.match(
    await card.locator('[data-action="apply"]').innerText(),
    /Older learning model/,
  );
  assert.match(
    await card.locator(".learn-status").innerText(),
    /Older learning model/,
  );
  assert.match(
    await card.locator(".learning-report").innerText(),
    /recorded scores have not been recalculated/,
  );
  assert.match(
    await card.locator('[data-action="learning-result"]').innerText(),
    /Older learning model/,
  );
  await page.evaluate(() => {
    const l = fixture.devices.a.last_learning;
    l.method = "human_priority_v10";
    fixture.devices.a.timing_configuration = structuredClone(
      l.timing.configuration,
    );
    l.estimated_training = structuredClone(l.training);
    l.automatic_evidence.used = true;
    return panel._load();
  });
  assert.match(
    await card.locator(".learning-report").innerText(),
    /Unweighted recorded-time cost/,
  );
  assert.match(
    await card.locator(".learning-report").innerText(),
    /Automatic search additionally weights time by confidence/,
  );
  assert.equal(await card.locator(".sample-outcomes").count(), 2);
  await page.evaluate(() => {
    for (const node of panel.shadowRoot.querySelectorAll(
      '[data-device-id="a"] details[data-detail]',
    ))
      node.open = window.beforeTimingDetails[node.dataset.detail] || false;
    for (const [key, open] of Object.entries(window.beforeTimingDetails))
      panel._sectionState.set(`a:detail-${key}`, open);
    panel._sectionState.set(
      "a:detail-evidence-review",
      window.beforeTimingDetails["evidence-review"] || false,
    );
    fixture.devices.a = window.beforeTiming;
    return panel._load();
  });
};
