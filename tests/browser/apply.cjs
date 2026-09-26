const assert = require("node:assert/strict");

module.exports = async function testApply(page, screenshotDir) {
  await testOutcomes(page, screenshotDir);
  await page.evaluate(() => {
    window.originalApplyWS = panel._hass.callWS;
    panel._hass.callWS = (message) => {
      if (message.type.endsWith("/apply")) {
        window.requests.push(message);
        return new Promise((resolve, reject) => {
          window.finishApply = resolve;
          window.failApply = reject;
        });
      }
      return window.originalApplyWS(message);
    };
    panel._setSectionCollapsed("a", "details", false);
    panel._draw();
  });
  const apply = page.locator('[data-device-id="a"] [data-action="apply"]');
  page.once("dialog", async (dialog) => {
    assert.match(dialog.message(), /Targets not met/);
    await dialog.accept();
  });
  await apply.click();
  await page.waitForFunction(() => Boolean(window.failApply));
  await page.evaluate(() => panel._load());
  assert.match(
    await page.locator('[data-device-id="a"] .action-status').innerText(),
    /Applying thresholds/,
  );
  assert.equal(await apply.getAttribute("aria-busy"), "true");
  assert.equal(
    await apply.isDisabled(),
    true,
    "polling must not replace an in-flight Apply button",
  );
  await page.evaluate(() =>
    window.failApply(new Error("No thresholds written: configuration changed")),
  );
  await page.waitForFunction(() => panel._activeActions === 0);
  assert.match(
    await page.locator("#error").innerText(),
    /No thresholds written/,
  );
  await page.evaluate(() => panel._load());
  assert.match(
    await page.locator("#error").innerText(),
    /No thresholds written/,
    "polling must retain the failure",
  );
  assert.equal(await apply.isDisabled(), false);

  page.once("dialog", (dialog) => dialog.accept());
  await apply.click();
  await page.waitForFunction(() => panel._activeActions === 1);
  await page.evaluate(() =>
    window.finishApply({
      applied: {},
      skipped: { g0_move: "requested 20, device reports 10" },
    }),
  );
  await page.waitForFunction(() => panel._activeActions === 0);
  assert.match(
    await page.locator("#error").innerText(),
    /Reported matches for 0 thresholds/,
  );
  await page.evaluate(() => {
    fixture.devices.a.last_learning = window.beforeApplyOutcomes;
    panel._hass.callWS = window.originalApplyWS;
    panel._actionError = "";
    panel._showError("");
  });
};

async function testOutcomes(page, screenshotDir) {
  await page.evaluate(() => {
    window.beforeApplyOutcomes = structuredClone(
      fixture.devices.a.last_learning,
    );
    panel._setSectionCollapsed("a", "details", false);
  });
  const apply = page.locator('[data-device-id="a"] [data-action="apply"]');
  const cases = [
    { name: "good", level: "good", color: "rgb(46, 125, 50)" },
    { name: "automatic", level: "caution", color: "rgb(251, 192, 45)" },
    { name: "backtest", level: "caution", color: "rgb(251, 192, 45)" },
    { name: "missing", level: "none", disabled: true },
    { name: "empty", level: "none", disabled: true },
    { name: "bad", level: "bad", color: "rgb(183, 28, 28)" },
  ];
  for (const scenario of cases) {
    await page.evaluate(({ name }) => {
      const learning = structuredClone(window.beforeApplyOutcomes);
      learning.status = "ok";
      learning.method = "human_priority_v6";
      learning.feasibility = { status: "not_ruled_out" };
      learning.training = { present_samples: 100, not_present_samples: 100 };
      learning.validation = {
        present_samples: 100,
        not_present_samples: 100,
        sensitivity: 1,
        false_positive_rate: 0,
      };
      if (name === "automatic") learning.training = null;
      if (name === "backtest") learning.validation.sensitivity = 0.9;
      if (name === "empty") learning.proposals = {};
      if (name === "bad") learning.status = "unsafe";
      fixture.devices.a.last_learning = name === "missing" ? null : learning;
      return panel._load();
    }, scenario);
    assert.equal(
      await apply.isDisabled(),
      Boolean(scenario.disabled),
      scenario.name,
    );
    assert.match(
      await apply.getAttribute("class"),
      new RegExp(`apply-${scenario.level}`),
    );
    if (scenario.color)
      assert.equal(
        await apply.evaluate(
          (button) => getComputedStyle(button).backgroundColor,
        ),
        scenario.color,
      );
    await apply.screenshot({
      path: require("node:path").join(
        screenshotDir,
        `apply-${scenario.name}.png`,
      ),
    });
  }
}
