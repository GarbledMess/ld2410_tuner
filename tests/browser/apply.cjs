const assert = require("node:assert/strict");

module.exports = async function testApply(page) {
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
  page.once("dialog", (dialog) => dialog.accept());
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
    panel._hass.callWS = window.originalApplyWS;
    panel._actionError = "";
    panel._showError("");
  });
};
