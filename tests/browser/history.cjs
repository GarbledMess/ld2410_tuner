const assert = require("node:assert/strict");

module.exports = async function testHistoryEditor(page) {
  await page.evaluate(() => {
    const timestamp = (value) => new Date(value).getTime() / 1000;
    window.savedPeriod = {
      start: timestamp("2026-09-23T09:00:23"),
      end: timestamp("2026-09-23T10:30:47"),
      state: "present",
    };
    fixture.devices.a.history = {
      revision: 8,
      labels: [
        {
          start: timestamp("2026-09-22T23:00:00"),
          end: timestamp("2026-09-23T01:00:00"),
          state: "unknown",
        },
        savedPeriod,
        {
          start: timestamp("2026-09-24T08:00:00"),
          end: timestamp("2026-09-24T09:00:00"),
          state: "not_present",
        },
      ],
    };
    panel._historyStateFor("a").day = "2026-09-23";
    panel._setSectionCollapsed("a", "history", false);
    window.originalWS = panel._hass.callWS;
    panel._hass.callWS = async (message) => {
      if (!message.type.endsWith("edit_history_label"))
        return originalWS(message);
      requests.push(message);
      if (window.rejectEdit)
        throw new Error(
          "History labels changed. Refresh and select the period again.",
        );
      const history = fixture.devices.a.history;
      history.labels = history.labels.filter(
        (label) =>
          label.start !== message.label_start ||
          label.end !== message.label_end,
      );
      if (message.state !== "unlabelled")
        history.labels.push({
          start: message.start,
          end: message.end,
          state: message.state,
        });
      history.revision++;
      return { ok: true };
    };
    return panel._load();
  });
  const card = page.locator('[data-device-id="a"]');
  const periods = card.locator('[data-action="history-edit"]');
  const day = card.locator('[data-action="history-day"]');
  assert.equal(
    await periods.count(),
    2,
    "Only periods intersecting the local day appear",
  );
  assert.equal(
    (await card.locator(".history-segment.unlabelled").count()) > 0,
    true,
  );
  assert.match(
    await card.locator('[data-role="calendar-month"]').innerText(),
    /September 2026/,
  );
  const selectedDay = card.locator(
    '[data-action="calendar-day"][data-day="2026-09-23"]',
  );
  assert.equal(await selectedDay.getAttribute("aria-pressed"), "true");
  assert.equal(await selectedDay.locator(".calendar-dots i").count(), 2);
  await card.locator('[data-action="calendar-next"]').click();
  assert.match(
    await card.locator('[data-role="calendar-month"]').innerText(),
    /October 2026/,
  );
  assert.equal(
    await day.inputValue(),
    "2026-09-23",
    "Browsing months must not change the selected timeline",
  );
  await card.locator('[data-action="calendar-previous"]').click();
  await card
    .locator('[data-action="calendar-day"][data-day="2026-09-24"]')
    .click();
  assert.equal(await day.inputValue(), "2026-09-24");
  assert.equal(await periods.count(), 1);
  await selectedDay.click();
  await card.locator('[data-action="history-next"]').click();
  assert.equal(await day.inputValue(), "2026-09-24");
  assert.equal(await periods.count(), 1);
  await card.locator('[data-action="history-previous"]').click();
  await periods.nth(1).click();
  const from = card.locator('[data-action="history-start"]');
  const to = card.locator('[data-action="history-end"]');
  assert.equal(await from.inputValue(), "2026-09-23T09:00:23");
  assert.equal(await to.inputValue(), "2026-09-23T10:30:47");
  await from.fill("2026-09-23T09:15:23");
  await from.blur();
  await page.evaluate(() => panel._load());
  assert.equal(
    await from.inputValue(),
    "2026-09-23T09:15:23",
    "Polling preserves edits and the selected period",
  );
  assert.equal(
    await card.locator('[data-action="history-apply"]').innerText(),
    "Save changes",
  );
  await card
    .locator('[data-action="history-state"]')
    .selectOption("not_present");
  page.once("dialog", (dialog) => dialog.accept());
  await card.locator('[data-action="history-apply"]').click();
  await page.waitForFunction(
    () => fixture.devices.a.history.revision === 9 && !panel._loading,
  );
  const saved = await page.evaluate(() =>
    requests.findLast((item) => item.type.endsWith("edit_history_label")),
  );
  assert.equal(saved.revision, 8);
  assert.equal(saved.state, "not_present");
  assert.equal(saved.start - saved.label_start, 15 * 60);
  assert.equal(
    saved.end,
    saved.label_end,
    "Unchanged boundary retains seconds exactly",
  );

  await periods.nth(1).click();
  await page.evaluate(() => {
    window.rejectEdit = true;
  });
  page.once("dialog", (dialog) => dialog.accept());
  await card.locator('[data-action="history-apply"]').click();
  await page
    .locator("#error")
    .filter({ hasText: "History labels changed" })
    .waitFor();
  assert.equal(
    await card.locator('[data-action="history-apply"]').innerText(),
    "Save changes",
    "Failed saves retain the edit",
  );
  assert.equal(await from.inputValue(), "2026-09-23T09:15:23");
  await page.evaluate(() => {
    window.rejectEdit = false;
  });

  page.once("dialog", (dialog) => dialog.dismiss());
  await card.locator('[data-action="history-remove"]').click();
  assert.equal(
    await periods.count(),
    2,
    "Canceling removal preserves the period",
  );
  page.once("dialog", (dialog) => dialog.accept());
  await card.locator('[data-action="history-remove"]').click();
  await page.waitForFunction(
    () => fixture.devices.a.history.revision === 10 && !panel._loading,
  );
  assert.equal(await periods.count(), 1);
  const removed = await page.evaluate(() =>
    requests.findLast((item) => item.type.endsWith("edit_history_label")),
  );
  assert.equal(
    removed.state,
    "unlabelled",
    "Removing a label is distinct from explicitly excluding it",
  );

  await periods.first().click();
  assert.equal(
    await from.inputValue(),
    "2026-09-22T23:00",
    "Cross-midnight periods retain their full original boundaries",
  );
  await card.locator('[data-action="history-new"]').click();
  assert.equal(await from.inputValue(), "");
  assert.equal(
    await card.locator('[data-action="history-remove"]').isVisible(),
    false,
  );
  await day.fill("2026-09-25");
  await day.blur();
  assert.equal(await periods.count(), 0);
  assert.match(
    await card.locator(".history-periods").innerText(),
    /No saved manual periods/,
  );

  const daylight = await page.evaluate(() => {
    const spring = panel._historyDayBounds("2026-03-29");
    const autumn = panel._historyDayBounds("2026-10-25");
    const original = Date.parse("2026-10-25T01:30:00Z") / 1000;
    const field = panel.shadowRoot.querySelector(
      '[data-device-id="a"] [data-action="history-start"]',
    );
    field.value = panel._historyInputValue(original);
    return {
      spring: (spring.end - spring.start) / 3600,
      autumn: (autumn.end - autumn.start) / 3600,
      preserved:
        panel._historyFieldTime(field.closest(".card"), "start", original) ===
        original,
    };
  });
  assert.deepEqual(daylight, { spring: 23, autumn: 25, preserved: true });
  await day.fill("2026-09-23");
  await day.blur();
  await periods.first().click();
  await page.evaluate(() => {
    panel._hass.callWS = originalWS;
  });
};
