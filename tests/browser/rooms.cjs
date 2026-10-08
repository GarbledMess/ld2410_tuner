const assert = require("node:assert/strict");

module.exports = async function testRooms(page) {
  await page.evaluate(() => {
    window.beforeRooms = panel._hass.callWS;
    window.savedRooms = structuredClone(window.fixture.rooms);
    window.fixture.rooms = {
      areas: { office: "Office" },
      members: {
        a: { name: "Desk radar", area_id: "office", recording_enabled: true },
        b: { name: "Sofa radar", area_id: "office", recording_enabled: true },
      },
      groups: {
        "area:office": {
          name: "Office",
          kind: "area",
          area_id: "office",
          device_ids: ["a", "b"],
          automatic: true,
          assessment: { state: "pending" },
        },
        "zone:desk": {
          name: "Desk",
          kind: "zone",
          area_id: "office",
          device_ids: ["a"],
          automatic: false,
          assessment: { state: "pending" },
        },
      },
    };
    panel._hass.callWS = async (message) => {
      if (message.type.endsWith("configure_room")) {
        window.requests.push(message);
        window.fixture.rooms.groups[message.group_id || "zone:new"] = {
          ...message,
          kind: "zone",
          automatic: false,
          assessment: { state: "pending" },
        };
        return {};
      }
      if (message.type.endsWith("assess_room")) {
        window.requests.push(message);
        window.fixture.rooms.groups[message.group_id].assessment = {
          state: "running",
        };
        return { state: "running" };
      }
      return window.beforeRooms(message);
    };
    return panel._load(true);
  });
  const rooms = page.locator("#rooms");
  await rooms.locator(":scope > summary").click();
  const office = rooms.locator('[data-room-id="area:office"]');
  const desk = rooms.locator('[data-room-id="zone:desk"]');
  assert.match(await office.innerText(), /Whole area/);
  assert.match(await desk.innerText(), /Independent zone/);
  await office.locator("summary").click();
  await desk.locator("summary").click();
  await rooms.locator('[data-room-action="add"]').click();
  const form = rooms.locator("form");
  await form.locator('[name="room-name"]').fill("Sofa <private>");
  await form.locator('[name="room-area"]').selectOption("office");
  await form.locator('[data-room-member="b"]').check();
  await form.locator('[name="room-name"]').blur();
  await page.evaluate(() => panel._load(true));
  assert.equal(
    await form.locator('[name="room-name"]').inputValue(),
    "Sofa <private>",
  );
  assert.equal(await form.locator('[data-room-member="b"]').isChecked(), true);
  await form.locator('[type="submit"]').click();
  await page.waitForFunction(() =>
    requests.some((r) => r.type.endsWith("configure_room")),
  );
  const saved = await page.evaluate(() =>
    requests.findLast((r) => r.type.endsWith("configure_room")),
  );
  assert.deepEqual(saved.device_ids, ["b"]);
  assert.equal(saved.area_id, "office");
  await page.waitForFunction(() => !panel._roomDraft);
  await desk.locator('[data-room-action="assess"]').click();
  await page.waitForFunction(
    () => panel._data.rooms.groups["zone:desk"].assessment.state === "running",
  );
  assert.match(await desk.innerText(), /You can leave and return/);
  assert.equal(
    await desk.locator('[data-room-action="assess"]').isDisabled(),
    true,
  );
  assert.equal(
    await office.locator('[data-room-action="assess"]').isDisabled(),
    false,
  );
  const stable = await page.evaluate(async () => {
    const node = panel.shadowRoot.querySelector('[data-room-id="zone:desk"]');
    const mutations = [];
    const observer = new MutationObserver((list) => mutations.push(...list));
    observer.observe(node, { childList: true, subtree: true });
    await panel._load(true);
    await panel._load(true);
    observer.disconnect();
    return {
      same:
        node === panel.shadowRoot.querySelector('[data-room-id="zone:desk"]'),
      mutations: mutations.length,
    };
  });
  assert.deepEqual(stable, { same: true, mutations: 0 });
  await page.evaluate(() => {
    const state = (seconds, active, samples, hits) => ({
      seconds,
      active_seconds: active,
      samples,
      hits,
    });
    window.fixture.rooms.groups["zone:desk"].assessment = {
      state: "ready",
      start: 1000,
      end: 4600,
      scorer_version: 1,
      room: {
        score: 99,
        basis: "human",
        presence_recall: 1,
        false_positive_percent: 1,
        target_met: true,
        outcomes: {
          human: {
            present: state(600, 600, 100, 100),
            not_present: state(600, 6, 100, 1),
          },
          automatic: {
            present: state(0, 0, 0, 0),
            not_present: state(0, 0, 0, 0),
          },
        },
      },
      devices: { a: { score: 99, exclusive_presence_seconds: 600 } },
      timing: {
        a: {
          timeout: 1,
          on_delay: null,
          off_delay: null,
          scope: "radar",
          mode: "device",
        },
      },
      excluded: { no_shared_recording_seconds: 2400, unlabelled_seconds: 0 },
      shared_recording_seconds: 1200,
    };
    return panel._load(true);
  });
  assert.match(await desk.innerText(), /Combined score 99.00\/100/);
  assert.match(await desk.innerText(), /1 false triggers \/ 99 correct empty/);
  assert.match(
    await desk.innerText(),
    /600.0s occupied time covered by this radar alone/,
  );
  assert.match(
    await desk.innerText(),
    /2400.0s without recordings from every member/,
  );
  assert.equal(
    await office.locator('[data-room-action="learn"]').count(),
    0,
    "whole area cannot mix independent zones while learning",
  );
  await page.evaluate(() => {
    const group = window.fixture.rooms.groups["zone:desk"];
    group.device_ids = ["a", "b"];
    group.learning = {
      slots: {
        user: {
          id: "joint-result",
          status: "unsafe",
          before: group.assessment,
          after: group.assessment,
          thresholds: { a: { g0_move: 35 }, b: { g0_move: 25 } },
          signature: {
            a: { thresholds: { g0_move: 20 } },
            b: { thresholds: { g0_move: 20 } },
          },
        },
      },
    };
    return panel._load(true);
  });
  assert.match(await desk.innerText(), /Desk radar.*20 → 35/);
  assert.match(await desk.innerText(), /Sofa radar.*20 → 25/);
  assert.equal(
    await desk.locator('[data-room-action="apply-joint"]').isDisabled(),
    false,
    "quality concerns do not prevent manual group Apply",
  );
  page.once("dialog", (dialog) => dialog.accept());
  await desk.locator('[data-room-action="apply-joint"]').click();
  await page.waitForFunction(() =>
    requests.some((r) => r.type.endsWith("apply_room")),
  );
  const joint = await page.evaluate(() =>
    requests.findLast((r) => r.type.endsWith("apply_room")),
  );
  assert.deepEqual(joint, {
    type: "ld2410_tuner/apply_room",
    group_id: "zone:desk",
    result_id: "joint-result",
    source: "user",
  });
  await page.setViewportSize({ width: 412, height: 850 });
  assert.equal(
    await rooms.locator('[data-room-id="zone:new"] summary').isVisible(),
    true,
  );
  await page.evaluate(() => {
    panel._hass.callWS = window.beforeRooms;
    window.fixture.rooms = window.savedRooms;
    panel._roomDraft = null;
    panel.shadowRoot.querySelector("#rooms").open = false;
    return panel._load(true);
  });
  await page.setViewportSize({ width: 1400, height: 1000 });
};
