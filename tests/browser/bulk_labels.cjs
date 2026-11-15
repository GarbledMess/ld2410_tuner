const assert = require("node:assert/strict");

// "Label several rooms and radars": one period, any mix of rooms, zones and radars, one
// request. The first time every room is ticked (and any radar in no room); afterwards the
// last selection. Named selections apply.
module.exports = async function testBulkLabels(page) {
  await page.evaluate(async () => {
    window.bulkBefore = structuredClone(fixture);
    window.bulkWS = panel._hass.callWS;
    delete fixture.label_selections;
    // c sits in an area with no room (it records nothing); d is in no area at all.
    fixture.devices.d = {
      ...structuredClone(fixture.devices.c),
      name: "Room d",
    };
    fixture.devices.c.recording_enabled = false;
    fixture.rooms = {
      areas: { lounge: "Lounge", hall: "Hall" },
      members: {
        a: { name: "Room a", area_id: "lounge", recording_enabled: true },
        b: { name: "Room b", area_id: "lounge", recording_enabled: true },
        c: { name: "Room c", area_id: "hall", recording_enabled: false },
        d: { name: "Room d", area_id: null, recording_enabled: true },
      },
      groups: {
        "area:lounge": {
          name: "Lounge",
          kind: "area",
          area_id: "lounge",
          device_ids: ["a", "b"],
        },
        "zone:sofa": {
          name: "Sofa",
          kind: "zone",
          area_id: "lounge",
          device_ids: ["a"],
        },
      },
    };
    panel._bulkDraft = null;
    panel._hass.callWS = async (message) => {
      const kind = message.type.split("/").pop();
      if (
        [
          "label_several",
          "save_label_template",
          "delete_label_template",
        ].includes(kind)
      ) {
        window.requests.push(message);
        const saved = (fixture.label_selections ||= {
          last: null,
          templates: [],
        });
        const chosen = {
          device_ids: [...(message.device_ids || [])].sort(),
          group_ids: [...(message.group_ids || [])].sort(),
        };
        if (kind === "label_several") {
          saved.last = chosen;
          return { ok: true, labelled: chosen };
        }
        saved.templates = saved.templates.filter(
          (t) => t.name !== message.name,
        );
        if (kind === "save_label_template")
          saved.templates.push({ name: message.name, ...chosen });
        return { ok: true };
      }
      return bulkWS(message);
    };
    await panel._load();
  });
  const card = page.locator("#bulk-labels");
  assert.equal(await card.count(), 1, "the card is on the page");
  await card.locator("summary").click();
  const ticked = () =>
    page.evaluate(() => {
      const on = (attribute) =>
        [...panel.shadowRoot.querySelectorAll(`[${attribute}]`)]
          .filter((box) => box.checked)
          .map((box) => box.getAttribute(attribute));
      return { groups: on("data-bulk-group"), radars: on("data-bulk-radar") };
    });
  assert.equal(
    await card.locator("[data-bulk-group]").count(),
    2,
    "room and zone",
  );
  assert.equal(
    await card.locator("[data-bulk-radar]").count(),
    4,
    "every radar",
  );
  assert.deepEqual(
    await ticked(),
    { groups: ["area:lounge"], radars: ["d"] },
    "every room the first time, and the recording radar in no area",
  );
  const hall = card.locator('[data-bulk-area="hall"]');
  assert.match(await hall.innerText(), /Hall/);
  assert.match(
    await hall.innerText(),
    /Room c \(not recording\)/,
    "a radar is listed under its area even where the area has no room",
  );
  const room = card.locator('[data-bulk-node="area:lounge"]');
  assert.equal(await room.locator('[data-bulk-group="zone:sofa"]').count(), 1);
  assert.equal(
    await room.locator("[data-bulk-radar]").count(),
    2,
    "radars under their room",
  );

  await page.evaluate(async () => {
    window.bulkGone = fixture.devices.d;
    delete fixture.devices.d;
    await panel._load();
    fixture.devices.d = bulkGone;
    await panel._load();
  });
  assert.deepEqual(
    (await ticked()).radars,
    ["d"],
    "until a selection is chosen, a radar that comes back is ticked too",
  );

  // As every other label editor: a Status dropdown, then "Label time range". Choosing a
  // status alone sends nothing.
  const mark = async (state) => {
    const before = (await sent()).length;
    await card.locator('[data-role="bulk-state"]').selectOption(state);
    assert.equal(
      (await sent()).length,
      before,
      "choosing a status sends nothing",
    );
    await card.locator('[data-action="bulk-apply"]').click();
  };
  assert.equal(
    await card.locator('[data-action="bulk-apply"]').innerText(),
    "Label time range",
  );
  assert.deepEqual(
    await card.locator('[data-role="bulk-state"] option').allInnerTexts(),
    ["Present", "Empty", "Unknown"],
  );
  const sent = () =>
    page.evaluate(() =>
      requests.filter((m) => m.type.endsWith("label_several")),
    );
  await card.locator('[data-bulk-group="zone:sofa"]').check();
  await card.locator('[data-bulk-radar="d"]').uncheck();
  await card.locator('[data-role="bulk-from"]').fill("2026-11-10T09:00");
  await card.locator('[data-role="bulk-to"]').fill("2026-11-10T08:00");
  await mark("not_present");
  assert.equal((await sent()).length, 0, "an end before the start is refused");
  assert.match(await card.locator('[role="status"]').innerText(), /after/);

  await card.locator('[data-role="bulk-to"]').fill("2026-11-10T10:30");
  await mark("not_present");
  await page.waitForFunction(() =>
    requests.some((m) => m.type.endsWith("label_several")),
  );
  const [request] = await sent();
  assert.deepEqual(request.group_ids, ["area:lounge", "zone:sofa"]);
  assert.deepEqual(request.device_ids, []);
  assert.equal(request.state, "not_present");
  const expected = await page.evaluate(() => [
    new Date("2026-11-10T09:00").getTime() / 1000,
    new Date("2026-11-10T10:30").getTime() / 1000,
  ]);
  assert.deepEqual([request.start, request.end], expected);
  await page.waitForFunction(() =>
    /2 rooms or zones/.test(
      panel.shadowRoot.querySelector('#bulk-labels [role="status"]')
        .textContent,
    ),
  );

  // A fresh panel shows the last selection.
  await page.evaluate(async () => {
    panel._bulkDraft = null;
    await panel._load();
  });
  assert.deepEqual(
    await ticked(),
    { groups: ["area:lounge", "zone:sofa"], radars: [] },
    "the last selection is remembered",
  );

  await card.locator('[data-action="bulk-none"]').click();
  assert.deepEqual(await ticked(), { groups: [], radars: [] });
  await mark("present");
  assert.equal((await sent()).length, 1, "nothing ticked, nothing sent");

  await card.locator('[data-bulk-radar="b"]').check();
  await card.locator('[data-bulk-group="zone:sofa"]').check();
  await card.locator('[data-role="bulk-name"]').fill("Sofa and b");
  await card.locator('[data-action="bulk-save"]').click();
  await page.waitForFunction(() =>
    panel.shadowRoot.querySelector(
      '[data-role="bulk-template"] option[value="Sofa and b"]',
    ),
  );
  const saves = await page.evaluate(() =>
    requests.filter((m) => m.type.endsWith("save_label_template")),
  );
  assert.deepEqual(
    saves.map((m) => [m.name, m.device_ids, m.group_ids]),
    [["Sofa and b", ["b"], ["zone:sofa"]]],
  );

  await card.locator('[data-action="bulk-none"]').click();
  await card.locator('[data-role="bulk-template"]').selectOption("Sofa and b");
  assert.deepEqual(
    await ticked(),
    { groups: ["zone:sofa"], radars: ["b"] },
    "a saved selection applies",
  );

  await card.locator('[data-action="bulk-delete"]').click();
  await page.waitForFunction(
    () =>
      !panel.shadowRoot.querySelector(
        '[data-role="bulk-template"] option[value="Sofa and b"]',
      ),
  );
  const deletes = await page.evaluate(() =>
    requests.filter((m) => m.type.endsWith("delete_label_template")),
  );
  assert.deepEqual(
    deletes.map((m) => m.name),
    ["Sofa and b"],
  );

  await page.evaluate(async () => {
    panel._hass.callWS = bulkWS;
    window.fixture = bulkBefore;
    panel._bulkDraft = null;
    await panel._load();
  });
};
