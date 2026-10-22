const assert = require("node:assert/strict");

// Room labels in a group's overview: timeline, Seen by chips, room presence
// sources, room conflicts, "Was this right?" and Remove confirmation.
const at = (time) => Date.parse(`2026-09-23T${time}:00+01:00`) / 1000;
const header = (page, id) =>
  page.locator(`#layout [data-group-id="${id}"]`).first();
const timeline = (page, id) =>
  header(page, id).locator(`[data-room-timeline="${id}"]`);
const lastRequest = (page, suffix) =>
  page.evaluate(
    (suffix) => requests.findLast((r) => r.type.endsWith(`/${suffix}`)),
    suffix,
  );
const SOURCES = {
  sources: [],
  mark_not_present: false,
  confidence: 90,
  start_buffer_seconds: 10,
  end_buffer_seconds: 10,
  state: "unknown",
  readings: [],
};

async function installFixture(page) {
  await page.evaluate((SOURCES) => {
    const at = (time) => Date.parse(`2026-09-23T${time}:00+01:00`) / 1000;
    window.realNow = Date.now;
    window.beforeRoomLabels = panel._hass.callWS;
    window.savedRoomLabelFixture = structuredClone(window.fixture);
    const group = (name, kind, area_id, device_ids, extra = {}) => ({
      name,
      kind,
      area_id,
      device_ids,
      automatic: kind === "area",
      assessment: { state: "pending" },
      labels: [],
      label_revision: 0,
      presence_sources: structuredClone(SOURCES),
      ...extra,
    });
    fixture.rooms = {
      areas: { office: "Office" },
      members: {
        a: { name: "Desk radar", area_id: "office", recording_enabled: true },
        b: { name: "Sofa radar", area_id: "office", recording_enabled: true },
        c: { name: "Spare radar", area_id: null, recording_enabled: true },
      },
      groups: {
        "area:office": group("Office", "area", "office", ["a", "b"], {
          labels: [
            {
              start: at("16:00"),
              end: at("17:00"),
              state: "not_present",
              source: "manual",
            },
            {
              start: at("16:00") + 2 * 86400,
              end: at("17:00") + 2 * 86400,
              state: "not_present",
              source: "manual",
            },
          ],
          label_revision: 2,
        }),
        "zone:desk": group("Desk", "zone", "office", ["a"], {
          labels: [
            {
              start: at("10:00"),
              end: at("11:00"),
              state: "present",
              source: "manual",
            },
            {
              start: at("10:30"),
              end: at("12:00"),
              state: "not_present",
              source: "sensor",
              confidence: 0.9,
            },
            {
              start: at("14:00"),
              end: at("15:00"),
              state: "not_present",
              source: "manual",
            },
            {
              start: at("16:30"),
              end: at("17:30"),
              state: "present",
              source: "manual",
            },
          ],
          label_revision: 4,
        }),
        "group:spare": group("Spare", "group", null, ["c"]),
      },
    };
    fixture.layout = [
      {
        id: "area:office",
        kind: "area",
        name: "Office",
        group_id: "area:office",
        devices: [],
        children: [
          {
            id: "zone:desk",
            kind: "zone",
            name: "Desk",
            group_id: "zone:desk",
            devices: ["a"],
            children: [],
          },
          {
            id: "area:office/rest",
            kind: "rest",
            name: "Elsewhere in Office",
            group_id: null,
            devices: ["b"],
            children: [],
          },
        ],
      },
      {
        id: "group:spare",
        kind: "group",
        name: "Spare",
        group_id: "group:spare",
        devices: ["c"],
        children: [],
      },
    ];
    fixture.room_attribution = {
      "zone:desk": [
        {
          start: at("10:00"),
          end: at("10:30"),
          estimated: false,
          devices: { a: { state: "yes", confidence: 0.9 } },
        },
      ],
      "area:office": [
        {
          start: at("10:00"),
          end: at("10:30"),
          devices: {
            a: { state: "yes", confidence: 0.5, inferred: true },
            b: { state: "unknown", confidence: 0, inferred: true },
          },
        },
        {
          start: at("18:00"),
          end: at("18:20"),
          estimated: true,
          devices: {
            a: { state: "no", confidence: 0.95, inferred: true },
            b: { state: "unknown", confidence: 0 },
            gone: { state: "unknown", confidence: 0 },
          },
        },
      ],
    };
    fixture.devices.a.history = {
      revision: 3,
      labels: [{ start: at("14:30"), end: at("15:30"), state: "present" }],
    };
    fixture.devices.b.history = {
      revision: 1,
      labels: [
        {
          start: at("16:30") + 2 * 86400,
          end: at("17:30") + 2 * 86400,
          state: "present",
        },
      ],
    };
    for (const id of ["a", "b", "c"])
      fixture.devices[id].auto_learning.last = null;
    const handled = [
      "label_room_range",
      "edit_room_label",
      "room_estimate_feedback",
      "configure_room_sources",
      "remove_room",
      "label_history",
    ];
    panel._hass.callWS = async (message) => {
      if (handled.some((type) => message.type.endsWith(`/${type}`))) {
        requests.push(message);
        return {};
      }
      return window.beforeRoomLabels(message);
    };
    panel._groupOpen.add("zone:desk");
    panel._groupOpen.add("area:office");
    panel._layoutOpen.set("area:office", true);
    panel._layoutOpen.set("zone:desk", true);
    return panel._load(true);
  }, SOURCES);
}

async function showDay(page, id) {
  const day = timeline(page, id).locator('[data-room-label-action="day"]');
  await day.fill("2026-09-23");
  await day.dispatchEvent("change");
  await day.blur();
  await page.waitForFunction(
    (id) => panel._historyStateFor(`room:${id}`).day === "2026-09-23",
    id,
  );
}

async function testTimelineLabelsARange(page) {
  const desk = timeline(page, "zone:desk");
  await showDay(page, "zone:desk");
  assert.equal(
    await desk.locator('[data-room-label-action="edit"]').count(),
    3,
    "every manual period on the day is editable; the sensor label is not",
  );
  assert.equal(
    await desk
      .locator(".room-sensor-labels .history-segment.sensor.not_present")
      .count(),
    1,
    "sensor labels are shown on their own read-only bar",
  );
  const bar = desk.locator(".history-range");
  await bar.scrollIntoViewIfNeeded();
  const box = await bar.boundingBox();
  await page.mouse.move(box.x + box.width * 0.2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * 0.7, box.y + box.height / 2, {
    steps: 4,
  });
  await page.mouse.up();
  const from = desk.locator('[data-action="history-start"]');
  assert.match(await from.inputValue(), /2026-09-23T04:/);
  assert.equal(
    await desk.locator(".history-range-selection").isVisible(),
    true,
  );
  await page.evaluate(() => panel._load(true));
  assert.match(
    await from.inputValue(),
    /2026-09-23T04:/,
    "the selected range survives a poll",
  );
  await from.fill("2026-09-23T05:00");
  await from.blur();
  await page.evaluate(() => panel._load(true));
  assert.equal(
    await from.inputValue(),
    "2026-09-23T05:00",
    "a typed time survives a poll",
  );
  await desk
    .locator('[data-room-label-action="state"]')
    .selectOption("not_present");
  page.once("dialog", (dialog) => dialog.accept());
  await desk.locator('[data-room-label-action="apply"]').click();
  await page.waitForFunction(() => panel._activeActions === 0);
  const request = await lastRequest(page, "label_room_range");
  assert.equal(request.group_id, "zone:desk");
  assert.equal(request.state, "not_present");
  assert.equal(request.start, at("05:00"));
  assert.ok(request.end >= at("16:00") && request.end < at("17:00"));
}

async function testEditAndRemoveManualPeriod(page) {
  const desk = timeline(page, "zone:desk");
  await showDay(page, "zone:desk");
  await page.evaluate(() => {
    const group = panel._data.rooms.groups["zone:desk"];
    group.labels = group.labels.filter((label) => label.state !== "present");
  });
  await desk.locator('[data-room-label-action="edit"]').first().click();
  await page
    .locator("#error")
    .filter({ hasText: "This saved period no longer exists" })
    .waitFor();
  await page.evaluate(() => panel._load(true));
  await desk.locator('[data-room-label-action="edit"]').first().click();
  assert.equal(
    await desk.locator('[data-action="history-start"]').inputValue(),
    "2026-09-23T10:00",
  );
  await desk
    .locator('[data-room-label-action="state"]')
    .selectOption("unknown");
  page.once("dialog", (dialog) => dialog.accept());
  await desk.locator('[data-room-label-action="apply"]').click();
  await page.waitForFunction(() => panel._activeActions === 0);
  assert.deepEqual(await lastRequest(page, "edit_room_label"), {
    type: "ld2410_tuner/edit_room_label",
    group_id: "zone:desk",
    label_start: at("10:00"),
    label_end: at("11:00"),
    start: at("10:00"),
    end: at("11:00"),
    state: "unknown",
    revision: 4,
  });
  await desk.locator('[data-room-label-action="edit"]').first().click();
  page.once("dialog", (dialog) => dialog.accept());
  await desk.locator('[data-room-label-action="remove"]').click();
  await page.waitForFunction(() => panel._activeActions === 0);
  const removed = await lastRequest(page, "edit_room_label");
  assert.equal(removed.state, "unlabelled");
  assert.equal(removed.label_start, at("10:00"));
  // Cancelled confirmations and incomplete ranges send nothing.
  const sent = () =>
    page.evaluate(
      () =>
        requests.filter(
          (r) =>
            r.type.endsWith("/label_room_range") ||
            r.type.endsWith("/edit_room_label"),
        ).length,
    );
  const before = await sent();
  await desk.locator('[data-room-label-action="edit"]').first().click();
  page.once("dialog", (dialog) => dialog.dismiss());
  await desk.locator('[data-room-label-action="remove"]').click();
  await page.waitForFunction(() => panel._activeActions === 0);
  page.once("dialog", (dialog) => dialog.dismiss());
  await desk.locator('[data-room-label-action="apply"]').click();
  await page.waitForFunction(() => panel._activeActions === 0);
  await desk.locator('[data-room-label-action="new"]').click();
  assert.equal(
    await desk.locator('[data-room-label-action="remove"]').isVisible(),
    false,
  );
  await desk.locator('[data-room-label-action="apply"]').click();
  await page
    .locator("#error")
    .filter({ hasText: "Choose a start and end time" })
    .waitFor();
  assert.equal(await sent(), before);
  const day = desk.locator('[data-room-label-action="day"]');
  await day.fill("");
  await day.dispatchEvent("change");
  assert.equal(
    await page.evaluate(() => panel._historyStateFor("room:zone:desk").day),
    "2026-09-23",
    "a cleared day field keeps the day",
  );
  // Day navigation moves the timeline without touching the server.
  await desk.locator('[data-room-label-action="next"]').click();
  assert.equal(
    await page.evaluate(() => panel._historyStateFor("room:zone:desk").day),
    "2026-09-24",
  );
  assert.equal(
    await desk.locator('[data-room-label-action="edit"]').count(),
    0,
  );
  assert.equal(
    await desk.locator(".seen-by").count(),
    0,
    "only the day's occupied periods are listed",
  );
  await desk.locator('[data-room-label-action="previous"]').click();
  assert.equal(
    await desk.locator('[data-room-label-action="edit"]').count(),
    3,
  );
  await desk.locator('[data-room-label-action="today"]').click();
  assert.notEqual(
    await page.evaluate(() => panel._historyStateFor("room:zone:desk").day),
    "2026-09-23",
  );
}

async function testSeenByChips(page) {
  await showDay(page, "zone:desk");
  await showDay(page, "area:office");
  const desk = timeline(page, "zone:desk");
  assert.match(await desk.locator(".seen-by").first().innerText(), /Seen by:/);
  const chip = desk.locator('[data-seen-by="a"]');
  assert.equal(await chip.innerText(), "Desk radar (high)");
  assert.equal(await chip.getAttribute("aria-pressed"), "true");
  assert.doesNotMatch(
    await desk.locator(".seen-by").first().innerText(),
    /estimated/,
  );
  await chip.click();
  await page.waitForFunction(() => panel._activeActions === 0);
  assert.deepEqual(await lastRequest(page, "label_history"), {
    type: "ld2410_tuner/label_history",
    device_id: "a",
    start: at("10:00"),
    end: at("10:30"),
    state: "not_present",
  });
  // Until the rebuilt attribution lands the chip shows it is updating.
  assert.equal(await chip.getAttribute("aria-busy"), "true");
  assert.equal(await chip.isDisabled(), true);
  assert.equal(await chip.innerText(), "Desk radar (high) · updating…");
  await page.evaluate(() => panel._load(true));
  assert.equal(await chip.getAttribute("aria-busy"), "true", "still pending");
  await page.evaluate(() => {
    fixture.room_attribution["zone:desk"][0].devices.a = {
      state: "no",
      confidence: 1,
    };
    return panel._load(true);
  });
  assert.equal(await chip.innerText(), "Desk radar: not seen (high)");
  assert.equal(await chip.getAttribute("aria-busy"), null);
  assert.equal(await chip.isDisabled(), false);
  const office = timeline(page, "area:office");
  const rows = office.locator(".seen-by");
  assert.equal(await rows.count(), 2);
  assert.match(await rows.nth(1).innerText(), /\(estimated\)/);
  assert.equal(
    await rows.first().locator('[data-seen-by="a"]').innerText(),
    "Desk radar (low, inferred)",
  );
  assert.equal(
    await rows.first().locator('[data-seen-by="b"]').innerText(),
    "Sofa radar (unknown)",
  );
  const unseen = rows.nth(1).locator('[data-seen-by="a"]');
  assert.equal(
    await unseen.innerText(),
    "Desk radar: not seen (high, inferred)",
  );
  assert.equal(await unseen.getAttribute("aria-pressed"), "false");
  await rows.first().locator('[data-seen-by="b"]').click();
  await page.waitForFunction(() => panel._activeActions === 0);
  const toggled = await lastRequest(page, "label_history");
  assert.equal(toggled.device_id, "b");
  assert.equal(toggled.state, "present");
  const fetches = () =>
    page.evaluate(
      () =>
        requests.filter(
          (r) =>
            r.type.endsWith("/history_series_multi") && r.device_id === "a",
        ).length,
    );
  const fetched = await fetches();
  await unseen.click();
  await page.waitForFunction(() => panel._activeActions === 0);
  assert.equal((await lastRequest(page, "label_history")).state, "present");
  await page.waitForFunction(() => !panel._chartState.get("a").loading);
  assert.ok(
    (await fetches()) > fetched,
    "a toggled radar's chart reloads with its new label",
  );
  // A radar missing from the registry keeps its id and has no chart to reset.
  const gone = rows.nth(1).locator('[data-seen-by="gone"]');
  assert.equal(await gone.innerText(), "gone (unknown)");
  await gone.click();
  await page.waitForFunction(() => panel._activeActions === 0);
  assert.equal((await lastRequest(page, "label_history")).device_id, "gone");
  // A rebuild that changes nothing must not leave a chip pending forever.
  assert.equal(await gone.getAttribute("aria-busy"), "true");
  await page.evaluate(() => {
    const now = Date.now();
    Date.now = () => now + 61000;
    return panel._load(true);
  });
  assert.equal(await gone.getAttribute("aria-busy"), null, "pending expires");
  await page.evaluate(() => {
    Date.now = window.realNow;
    return panel._load(true);
  });
}

async function testRoomSources(page) {
  await header(page, "zone:desk")
    .locator(".room-settings")
    .evaluate((node) => {
      node.open = true;
    });
  const sources = header(page, "zone:desk").locator(".presence-sources");
  assert.match(await sources.locator("summary").innerText(), /Room presence/);
  await sources.locator("summary").click();
  await sources.locator('[data-action="source-add"]').click();
  await sources
    .locator("[data-source-entity]")
    .fill("binary_sensor.desk_camera");
  await sources.locator('[data-action="sources-save"]').click();
  await page.waitForFunction(() => panel._activeActions === 0);
  assert.deepEqual(await lastRequest(page, "configure_room_sources"), {
    type: "ld2410_tuner/configure_room_sources",
    group_id: "zone:desk",
    settings: {
      sources: [
        { kind: "boolean", entity_id: "binary_sensor.desk_camera", area: "" },
      ],
      mark_not_present: false,
      confidence: 90,
      start_buffer_seconds: 10,
      end_buffer_seconds: 10,
    },
  });
  assert.equal(
    await page.evaluate(() =>
      requests.some((r) => r.type.endsWith("/configure_presence_sources")),
    ),
    false,
    "room sources never reach a radar's own sources",
  );
}

async function testConflicts(page) {
  const desk = await timeline(page, "zone:desk")
    .locator(".room-conflicts")
    .innerText();
  assert.match(desk, /Reviewing conflicting labels/);
  assert.match(desk, /room Empty vs radar Present \(Desk radar\)/);
  assert.match(desk, /zone Present inside area Empty \(Office\)/);
  assert.match(desk, /room Present vs sensor Empty/);
  const officeTimeline = timeline(page, "area:office");
  const office = await officeTimeline.locator(".room-conflicts").innerText();
  assert.match(office, /zone Present \(Desk\) inside area Empty/);
  assert.doesNotMatch(
    office,
    /Sofa radar/,
    "another day's conflict is not listed",
  );
  await officeTimeline.locator('[data-room-label-action="next"]').click();
  await officeTimeline.locator('[data-room-label-action="next"]').click();
  assert.equal(await officeTimeline.locator(".room-conflicts li").count(), 1);
  assert.match(
    await officeTimeline.locator(".room-conflicts").innerText(),
    /room Empty vs radar Present \(Sofa radar\)/,
  );
  await showDay(page, "area:office");
  assert.equal(
    await timeline(page, "group:spare").locator(".room-conflicts").count(),
    0,
    "no conflicts, no list",
  );
  assert.equal(
    await timeline(page, "zone:desk").locator(".room-conflicts li").count(),
    3,
    "only overlapping labels conflict",
  );
  assert.equal(
    await timeline(page, "group:spare").locator(".room-sensor-labels").count(),
    0,
    "no sensor labels, no sensor bar",
  );
}

async function testEstimateFeedback(page) {
  const desk = timeline(page, "zone:desk");
  const office = timeline(page, "area:office");
  // Fresh radar guesses alone never make the panel invent an estimate.
  await page.evaluate(() => {
    fixture.devices.a.auto_learning.last = {
      state: "present",
      confidence: 0.9,
    };
    return panel._load(true);
  });
  assert.equal(
    await desk.locator(".room-estimate").count(),
    0,
    "no server estimate, none shown",
  );
  const estimate = (state, confidence) =>
    page.evaluate(
      ({ state, confidence, start }) => {
        Date.now = () => 0; // A wrong browser clock changes nothing.
        fixture.rooms.groups["zone:desk"].estimate = {
          state,
          confidence,
          start,
          end: start + 1200,
        };
        return panel._load(true);
      },
      { state, confidence, start: at("10:00") },
    );
  await estimate("present", 0.73);
  assert.match(
    await desk.locator(".room-estimate").innerText(),
    /Room estimate: Present \(73%\) since 10:00.*Was this right\?/,
  );
  assert.equal(await office.locator(".room-estimate").count(), 0);
  await desk.locator('[data-room-estimate="true"]').click();
  await page.waitForFunction(() => panel._activeActions === 0);
  assert.deepEqual(await lastRequest(page, "room_estimate_feedback"), {
    type: "ld2410_tuner/room_estimate_feedback",
    group_id: "zone:desk",
    correct: true,
  });
  await estimate("not_present", 0.6);
  assert.match(
    await desk.locator(".room-estimate").innerText(),
    /Room estimate: Empty \(60%\)/,
  );
  await desk.locator('[data-room-estimate="false"]').click();
  await page.waitForFunction(() => panel._activeActions === 0);
  assert.deepEqual(await lastRequest(page, "room_estimate_feedback"), {
    type: "ld2410_tuner/room_estimate_feedback",
    group_id: "zone:desk",
    correct: false,
  });
  await page.evaluate(() => {
    Date.now = window.realNow;
    fixture.devices.a.auto_learning.last = null;
    delete fixture.rooms.groups["zone:desk"].estimate;
    return panel._load(true);
  });
}

async function testRemoveAsksWhenLabelled(page) {
  await header(page, "area:office").locator(".room-settings > summary").click();
  const count = () =>
    page.evaluate(
      () => requests.filter((r) => r.type.endsWith("/remove_room")).length,
    );
  const before = await count();
  page.once("dialog", async (dialog) => {
    assert.match(dialog.message(), /room labels \(4\)/);
    await dialog.dismiss();
  });
  await header(page, "zone:desk")
    .locator('[data-room-action="remove"]')
    .click();
  await page.waitForFunction(() => panel._activeActions === 0);
  assert.equal(await count(), before, "cancelled: nothing is sent");
  page.once("dialog", (dialog) => dialog.accept());
  await header(page, "zone:desk")
    .locator('[data-room-action="remove"]')
    .click();
  await page.waitForFunction(() => panel._activeActions === 0);
  assert.equal(await count(), before + 1);
  // A group without room labels is removed at once (an unexpected dialog
  // would be dismissed and nothing would be sent).
  await page.evaluate(() => {
    panel._groupOpen.add("group:spare");
    panel._layoutOpen.set("group:spare", true);
    return panel._load(true);
  });
  await header(page, "group:spare").locator(".room-settings > summary").click();
  await header(page, "group:spare")
    .locator('[data-room-action="remove"]')
    .click();
  await page.waitForFunction(() => panel._activeActions === 0);
  assert.equal(await count(), before + 2);
  // Restoring an area default keeps its room labels, so it needs no prompt.
  await page.evaluate(() => {
    fixture.rooms.groups["area:office"].automatic = false;
    return panel._load(true);
  });
  await header(page, "area:office")
    .locator(".room-settings")
    .evaluate((node) => {
      node.open = true;
    });
  await header(page, "area:office")
    .locator('[data-room-action="remove"]')
    .first()
    .click();
  await page.waitForFunction(() => panel._activeActions === 0);
  assert.equal(await count(), before + 3);
}

async function testHelpAndMobile(page) {
  const help = await page
    .locator("details.layout-help")
    .evaluate((node) => node.textContent);
  // One line in the panel; Seen by and scoring are described in the README.
  assert.match(help, /Label a room or zone when it is occupied or empty/);
  assert.match(help, /See the README for how labels combine/);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => panel._load(true));
  assert.ok(
    (await page.evaluate(() => document.documentElement.scrollWidth)) <= 390,
    "open room timelines have no horizontal scroll at 390px",
  );
  await page.setViewportSize({ width: 1400, height: 1000 });
}

module.exports = async function testRoomLabels(page) {
  await installFixture(page);
  await testTimelineLabelsARange(page);
  await testEditAndRemoveManualPeriod(page);
  await testSeenByChips(page);
  await testRoomSources(page);
  await testConflicts(page);
  await testEstimateFeedback(page);
  await testRemoveAsksWhenLabelled(page);
  await testHelpAndMobile(page);
  await page.evaluate(() => {
    panel._hass.callWS = window.beforeRoomLabels;
    window.fixture = window.savedRoomLabelFixture;
    for (const id of ["zone:desk", "area:office", "group:spare"])
      panel._groupOpen.delete(id);
    return panel._load(true);
  });
};
