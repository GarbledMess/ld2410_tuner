const assert = require("node:assert/strict");

// "Radar timeout": the last Learn's timeout scores, the suggested and current ones marked, what
// Learn did with it, and the per-radar automatic timeout override; the global default sits in
// the learning settings beside automatic Apply.
module.exports = async function testTimeoutSuggestion(page) {
  await page.evaluate(async () => {
    window.timeoutWS = panel._hass.callWS;
    const row = (timeout, dark, missed, falseSeconds, starts) => ({
      timeout,
      dark_spells: dark,
      missed_seconds: missed,
      false_seconds: falseSeconds,
      false_starts: starts,
      cost: 0,
    });
    panel._hass.callWS = async (message) => {
      if (
        message.type.endsWith("/configure_device_timeout_apply") ||
        message.type.endsWith("/configure_learning_schedule")
      ) {
        window.requests.push(message);
        return {};
      }
      const result = await timeoutWS(message);
      if (message.type.endsWith("/snapshot") && result?.devices?.a) {
        result.devices.a.timeout_apply_policy = {
          override: "inherit",
          global: "off",
          effective: "off",
        };
        result.devices.a.timeout_suggestion = {
          status: "applied",
          source: "user",
          current_timeout: 1,
          previous_timeout: 1,
          applied_timeout: 8,
          suggestion: {
            timeout: 8,
            cheapest: 12,
            keep: 0.8,
            candidates: [
              row(1, 14, 54, 2154, 415),
              row(2, 12, 42, 2328, 412),
              row(3, 8, 36, 2484, 403),
              row(5, 6, 18, 2742, 395),
              row(8, 3, 6, 3078, 389),
              row(12, 1, 0, 3492, 385),
            ],
          },
        };
      }
      return result;
    };
    panel._sectionState.set("a:details", false);
    await panel._load(true);
    const details = panel.shadowRoot.querySelector(
      '[data-device-id="a"] details[data-detail="learning-details"]',
    );
    if (details) details.open = true;
  });
  const card = page.locator('#layout .card[data-device-id="a"]').first();
  const block = card.locator(".timeout-suggestion");
  assert.equal(await block.count(), 1, "the block is in the radar's details");
  assert.equal(
    await block.locator('[data-action="suggest-timeout"]').count(),
    0,
    "Learn scores the timeouts; there is no separate check",
  );
  const rows = await block.locator("tbody tr").allInnerTexts();
  assert.equal(rows.length, 6);
  assert.match(rows[4], /8 s.*Suggested/s);
  assert.match(rows[0], /1 s.*Was/s);
  assert.match(rows[5], /12 s.*Cheapest/s);
  assert.match(
    await block.locator('[role="status"]').innerText(),
    /Learn set the radar's timeout to 8 s \(was 1 s\), the shortest that keeps 80% of the best timeout's improvement/,
  );

  const override = block.locator('[data-action="device-timeout-apply"]');
  assert.match(await override.innerText(), /Inherit global \(Off\)/);
  await override.selectOption("all");
  await page.waitForFunction(() =>
    requests.some((m) => m.type.endsWith("/configure_device_timeout_apply")),
  );
  const sent = await page.evaluate(() =>
    requests.filter((m) => m.type.endsWith("/configure_device_timeout_apply")),
  );
  assert.deepEqual(
    sent.map((m) => [m.device_id, m.mode]),
    [["a", "all"]],
  );

  const before = await page.evaluate(
    () =>
      requests.filter((m) => m.type.endsWith("/configure_learning_schedule"))
        .length,
  );
  const settings = page.locator("#learning-schedule");
  await settings.locator('[data-action="timeout-apply"]').check();
  await settings
    .locator('[data-action="timeout-apply-scope"]')
    .selectOption("all");
  await settings.locator('[data-action="timeout-keep"]').fill("90");
  await settings.locator('[data-action="nightly-save"]').click();
  await page.waitForFunction(
    (count) =>
      requests.filter((m) => m.type.endsWith("/configure_learning_schedule"))
        .length > count,
    before,
  );
  const saved = await page.evaluate(() =>
    requests.findLast((m) => m.type.endsWith("/configure_learning_schedule")),
  );
  assert.equal(saved.timeout_apply, true);
  assert.equal(saved.timeout_apply_scope, "all");
  assert.equal(saved.timeout_keep, 90);

  await page.evaluate(async () => {
    panel._hass.callWS = timeoutWS;
    panel._scheduleDraft = null;
    await panel._load(true);
  });
};
