const assert = require("node:assert/strict");
const path = require("node:path");

module.exports = async function testStorage(page, screenshotDir) {
  await page.evaluate(async () => {
    window.storageWS = panel._hass.callWS;
    window.beforeStorage = structuredClone(fixture);
    fixture.storage = {
      settings: {
        thin_after_days: 7,
        minimum_confidence: 50,
        automatic_days: 30,
        human_days: 30,
        max_mib: 100,
      },
      used_bytes: 2097152,
    };
    fixture.devices.a.recording_enabled = false;
    panel._hass.callWS = async (message) => {
      if (message.type.endsWith("/set_recording")) {
        requests.push(message);
        if (window.failRecording)
          throw new Error("Recording setting could not be saved");
        fixture.devices[message.device_id].recording_enabled = message.enabled;
        return { enabled: message.enabled };
      }
      if (message.type.endsWith("/configure_storage")) {
        requests.push(message);
        fixture.storage.settings = structuredClone(message.settings);
        return {};
      }
      if (message.type.endsWith("/trim_storage")) {
        requests.push(message);
        return new Promise((resolve) => {
          window.finishTrim = resolve;
        });
      }
      return storageWS(message);
    };
    panel._storageOpen = true;
    panel._collapsed.add("a");
    await panel._load();
  });
  const card = page.locator('[data-device-id="a"]');
  const form = page.locator("#storage-controls");
  assert.equal(await page.locator("[data-storage=human_days]").count(), 1);
  const toggle = card.locator('[data-action="recording"]');
  assert.equal(await toggle.isVisible(), true);
  assert.equal(await toggle.isChecked(), false);
  assert.match(await card.locator(".recording-control").innerText(), /Paused/);
  assert.equal(await card.locator('[data-action="state"]').isDisabled(), true);
  await toggle.check();
  await page.waitForFunction(() => panel._activeActions === 0);
  assert.equal(await toggle.isChecked(), true);
  assert.equal(await card.locator('[data-action="state"]').isDisabled(), false);
  await page.evaluate(() => panel._load());
  assert.equal(await toggle.isChecked(), true);
  await page.evaluate(() => {
    window.failRecording = true;
  });
  await toggle.click();
  await page.waitForFunction(() => panel._activeActions === 0);
  assert.equal(await toggle.isChecked(), true);
  assert.match(await page.locator("#error").innerText(), /could not be saved/);
  await page.evaluate(() => {
    window.failRecording = false;
  });
  await toggle.uncheck();
  await page.waitForFunction(() => panel._activeActions === 0);
  assert.equal(await toggle.isChecked(), false);

  assert.equal(
    await form.locator('[data-storage="thin_after_days"]').inputValue(),
    "7",
  );
  assert.equal(
    await form.locator('[data-storage="minimum_confidence"]').inputValue(),
    "50",
  );
  await form.locator('[data-storage="human_days"]').fill("90");
  await form.locator('[data-storage="max_mib"]').fill("25");
  await page.evaluate(() => panel.shadowRoot.activeElement.blur());
  await page.evaluate(() => panel._load());
  assert.equal(
    await form.locator('[data-storage="human_days"]').inputValue(),
    "90",
  );
  page.once("dialog", (dialog) => dialog.accept());
  await form.locator('[data-action="storage-save"]').click();
  await page.waitForFunction(() => panel._activeActions === 0);
  assert.deepEqual(await page.evaluate(() => fixture.storage.settings), {
    thin_after_days: 7,
    minimum_confidence: 50,
    automatic_days: 30,
    human_days: 90,
    max_mib: 25,
  });
  assert.match(
    await form.locator(".storage-status").innerText(),
    /2.00 MiB \/ 25 MiB/,
  );
  await form.locator('[data-action="trim-target"]').fill("10");
  page.once("dialog", (dialog) => dialog.dismiss());
  await form.locator('[data-action="storage-trim"]').click();
  await page.waitForFunction(() => panel._activeActions === 0);
  assert.equal(
    await page.evaluate(() =>
      requests.some((r) => r.type.endsWith("/trim_storage")),
    ),
    false,
  );
  page.once("dialog", (dialog) => dialog.accept());
  await form.locator('[data-action="storage-trim"]').click();
  await page.waitForFunction(() => !!window.finishTrim);
  assert.match(
    await form.locator(".storage-action-status").innerText(),
    /Trimming recorded data/,
  );
  await page.evaluate(() => panel._load());
  assert.match(
    await form.locator(".storage-action-status").innerText(),
    /Trimming recorded data/,
  );
  assert.equal(
    await form.locator('[data-action="storage-trim"]').isDisabled(),
    true,
  );
  assert.equal(
    await page.evaluate(
      () => requests.find((r) => r.type.endsWith("/trim_storage")).target_mib,
    ),
    10,
  );
  await page.evaluate(() => {
    fixture.storage.used_bytes = 1048576;
    fixture.storage.last_trim = { removed_automatic: 600, removed_human: 0 };
    finishTrim({});
  });
  await page.waitForFunction(() => panel._activeActions === 0);
  assert.match(
    await form.locator(".storage-status").innerText(),
    /600 automatic\/unlabelled and 0 human-labelled/,
  );
  await page.setViewportSize({ width: 390, height: 844 });
  await form.screenshot({
    path: path.join(screenshotDir, "storage-mobile.png"),
  });
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
    true,
  );
  await page.evaluate(async () => {
    fixture = beforeStorage;
    panel._hass.callWS = storageWS;
    panel._storageDraft = null;
    panel._storageOpen = false;
    panel._trimTarget = "";
    panel._collapsed.delete("a");
    await panel._load();
  });
  await page.setViewportSize({ width: 1400, height: 1000 });
};
