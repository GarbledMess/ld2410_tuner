const assert = require("node:assert/strict");

// A control that cannot be used says why, next to it, instead of only greying out.
module.exports = async function testDisabledReasons(page) {
  const described = (root, selector) =>
    root.evaluate((node, selector) => {
      const control = node.querySelector(selector);
      const reason = control?.getAttribute("aria-describedby");
      const note = reason && node.getRootNode().getElementById(reason);
      return {
        disabled: !!control?.disabled,
        reason: note && !note.hidden ? note.textContent.trim() : "",
      };
    }, selector);

  // Presence labelling needs recording.
  await page.evaluate(() => {
    fixture.devices.a.recording_enabled = false;
    return panel._load(true);
  });
  const card = page.locator('[data-device-id="a"]').first();
  try {
    for (const action of ["state", "timeout-hours", "timeout-minutes"]) {
      const result = await described(card, `[data-action="${action}"]`);
      assert.equal(result.disabled, true, `${action} is unavailable`);
      assert.match(result.reason, /recording is off/i, `${action} says why`);
    }
  } finally {
    await page.evaluate(() => {
      fixture.devices.a.recording_enabled = true;
      return panel._load(true);
    });
  }
  const enabled = await described(card, '[data-action="state"]');
  assert.deepEqual(enabled, { disabled: false, reason: "" });

  // Automatic Apply scope needs automatic Apply.
  const schedule = page.locator("#learning-schedule");
  const scope = async (checked) => {
    await schedule.evaluate((node, checked) => {
      const box = node.querySelector('[data-action="nightly-auto-apply"]');
      box.checked = checked;
      box.dispatchEvent(new Event("input", { bubbles: true }));
    }, checked);
    return described(schedule, '[data-action="auto-apply-scope"]');
  };
  const off = await scope(false);
  assert.equal(off.disabled, true);
  assert.match(off.reason, /automatic apply/i);
  assert.deepEqual(await scope(true), { disabled: false, reason: "" });
  await page.evaluate(() => {
    panel._scheduleDraft = null;
    panel._drawLearningSchedule();
  });

  // Timing values are only used as defaults.
  const timing = page.locator("#timing-controls");
  const mode = async (value) => {
    await timing.evaluate((node, value) => {
      const select = node.querySelector('[data-action="timing-mode"]');
      select.value = value;
      select.dispatchEvent(new Event("change", { bubbles: true }));
    }, value);
    return described(timing, "[data-timing]");
  };
  const device = await mode("device");
  assert.equal(device.disabled, true);
  assert.match(device.reason, /use defaults where device timing is missing/i);
  assert.deepEqual(await mode("fallback"), { disabled: false, reason: "" });
  await page.evaluate(() => {
    panel._timingDraft = null;
    panel._drawTimingSettings();
  });

  // A whole-area group keeps its area.
  const area = await page.evaluate(() => {
    const previous = panel._roomDraft;
    panel._roomDraft = {
      group_id: "area:office",
      name: "Office",
      area_id: "office",
      device_ids: [],
    };
    const host = document.createElement("div");
    host.innerHTML = panel._roomEditorHtml({
      areas: { office: "Office" },
      members: {},
    });
    panel._roomDraft = previous;
    document.body.append(host);
    const select = host.querySelector('[name="room-area"]');
    const note = host.querySelector(
      `#${CSS.escape(select.getAttribute("aria-describedby"))}`,
    );
    host.remove();
    return { disabled: select.disabled, reason: note?.textContent || "" };
  });
  assert.equal(area.disabled, true);
  assert.match(area.reason, /whole-area group/i);

  // Presence sources stop at their limit.
  const sources = await page.evaluate(() => {
    const host = document.createElement("div");
    const sources = Array.from({ length: 16 }, (_, index) => ({
      entity_id: `binary_sensor.s${index}`,
    }));
    host.innerHTML = panel._sourcesHtml("limit", {
      presence_sources: { sources },
    });
    document.body.append(host);
    const add = host.querySelector('[data-action="source-add"]');
    const id = add.getAttribute("aria-describedby");
    const note = id && host.querySelector(`#${CSS.escape(id)}`);
    host.remove();
    return { disabled: add.disabled, reason: note?.textContent || "" };
  });
  assert.equal(sources.disabled, true);
  assert.match(sources.reason, /16/);
};
