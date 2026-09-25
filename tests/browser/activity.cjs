const assert = require("node:assert/strict");

module.exports = async function testActivity(page) {
  const card = page.locator('[data-device-id="a"]');
  await page.evaluate(() => {
    window.activityWS = panel._hass.callWS;
    window.activityDownload = panel._download;
    panel._download = () => {};
    panel._setSectionCollapsed("a", "actions", false);
    panel._setSectionCollapsed("a", "auto", false);
    panel._draw();
    panel._hass.callWS = (message) => {
      if (message.type.endsWith("/" + window.pendingAction)) {
        requests.push(message);
        return new Promise((resolve, reject) => {
          window.finishActivity = resolve;
          window.failActivity = reject;
        });
      }
      return activityWS(message);
    };
  });
  for (const [action, endpoint, label] of [
    ["learn", "learn", "Learning thresholds"],
    ["json", "export", "Preparing JSON"],
    ["csv", "export", "Preparing CSV"],
    ["auto-feedback", "auto_feedback", "Saving feedback"],
  ]) {
    await page.evaluate((name) => {
      window.pendingAction = name;
      window.finishActivity = null;
    }, endpoint);
    const control = card.locator(`[data-action="${action}"]`).first();
    await control.click();
    await page.waitForFunction(() => !!window.finishActivity);
    assert.match(
      await card.locator(".action-status").innerText(),
      new RegExp(label),
    );
    assert.equal(
      await card
        .locator(".action-status")
        .evaluate((el) => getComputedStyle(el, "::before").animationName),
      "busy-spin",
    );
    assert.equal(
      await card.locator('[data-action="clear"]').isDisabled(),
      true,
    );
    assert.equal(
      await card.locator('[data-action="state"]').isDisabled(),
      true,
    );
    await page.evaluate(() => panel._load());
    assert.equal(await control.getAttribute("aria-busy"), "true");
    // A second invocation in this card cannot race the pending command.
    const before = await page.evaluate(() => requests.length);
    await control.evaluate((el) => el.onclick({ currentTarget: el }));
    assert.equal(await page.evaluate(() => requests.length), before);
    await page.evaluate(() => window.finishActivity({ devices: {} }));
    await page.waitForFunction(() => panel._activeActions === 0);
    assert.equal(await card.locator(".action-status").innerText(), "");
    assert.equal(await control.isDisabled(), false);
  }
  // An export failure follows the same visible error and cleanup path.
  await page.evaluate(() => {
    window.pendingAction = "export";
    window.failActivity = null;
  });
  await card.locator('[data-action="json"]').click();
  await page.waitForFunction(() => !!window.failActivity);
  await page.evaluate(() =>
    window.failActivity(new Error("Export unavailable")),
  );
  await page.waitForFunction(() => panel._activeActions === 0);
  assert.match(await page.locator("#error").innerText(), /Export unavailable/);
  assert.equal(await card.locator(".is-busy").count(), 0);

  await page.evaluate(() => {
    window.activityCall = panel._call;
    panel._call = function (type, data, timeout) {
      return activityCall.call(
        this,
        type,
        data,
        type === "export" ? 40 : timeout,
      );
    };
  });
  await card.locator('[data-action="json"]').click();
  await page.waitForFunction(() => panel._activeActions === 0);
  assert.match(await page.locator("#error").innerText(), /timed out/);
  assert.equal(await card.locator(".is-busy").count(), 0);
  assert.equal(await card.locator('[data-action="json"]').isDisabled(), false);
  await page.evaluate(() => {
    panel._call = activityCall;
    window.finishActivity({});
  });

  // Background refresh has a delayed indicator and keeps the cards in place.
  await page.evaluate(() => {
    window.pendingAction = "snapshot";
    window.finishActivity = null;
    panel._load();
  });
  await page.locator("#snapshot-status.is-busy").waitFor();
  assert.match(
    await page.locator("#snapshot-status").innerText(),
    /Refreshing device readings/,
  );
  assert.equal(await page.locator(".card").count(), 3);
  await page.evaluate(() => window.finishActivity(structuredClone(fixture)));
  await page.waitForFunction(() => !panel._loading);
  assert.equal(await page.locator("#snapshot-status").innerText(), "");
  await page.evaluate(() => {
    window.pendingAction = null;
    panel._hass.callWS = activityWS;
    panel._download = activityDownload;
    panel._actionError = "";
    panel._showError("");
  });
  await page.evaluate(() => {
    const initial = document.createElement("ld2410-tuner-panel");
    window.initialPanel = initial;
    document.body.append(initial);
    initial.hass = {
      callWS: () =>
        new Promise((resolve) => {
          window.finishInitial = resolve;
        }),
    };
  });
  const initial = page.locator("ld2410-tuner-panel").nth(1);
  await initial.locator("#snapshot-status.is-busy").waitFor();
  assert.match(
    await initial.locator("#snapshot-status").innerText(),
    /Loading devices/,
  );
  await page.evaluate(() => window.finishInitial({ devices: {} }));
  await page.waitForFunction(() => !initialPanel._loading);
  assert.equal(await initial.locator(".is-busy").count(), 0);
  await page.evaluate(() => initialPanel.remove());
};
