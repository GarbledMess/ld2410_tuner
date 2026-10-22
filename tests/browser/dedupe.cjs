const assert = require("node:assert/strict");

// Nothing on the page repeats the element directly above it.
module.exports = async function testDedupe(page) {
  await page.evaluate(() => {
    window.dedupeOriginal = structuredClone(fixture);
    panel._shownZones.clear();
  });
  try {
    await testIdenticalZoneHidden(page);
    await testLoneRadarEditor(page);
    await testSourcesBelongToGroups(page);
    await testCalendarRemoved(page);
  } finally {
    await page.evaluate(() => {
      window.fixture = structuredClone(dedupeOriginal);
      return panel._load(true);
    });
  }
};

function roomFixture(zoneIds, zoneLabels = []) {
  const group = (name, kind, ids, labels = []) => ({
    name,
    kind,
    area_id: "ux",
    device_ids: ids,
    labels,
    automatic: kind === "area",
    assessment: { state: "pending" },
    presence_sources: { sources: [] },
  });
  fixture.rooms = {
    areas: { ux: "Office" },
    members: {
      a: { name: "Desk radar", area_id: "ux", recording_enabled: true },
      b: { name: "Door radar", area_id: "ux", recording_enabled: true },
    },
    groups: {
      "area:ux": group("Office", "area", ["a", "b"]),
      "zone:ux": group("Desk", "zone", zoneIds, zoneLabels),
    },
  };
  fixture.layout = [
    {
      id: "area:ux",
      name: "Office",
      kind: "area",
      group_id: "area:ux",
      devices: ["a", "b"].filter((id) => !zoneIds.includes(id)),
      children: [
        {
          id: "zone:ux",
          name: "Desk",
          kind: "zone",
          group_id: "zone:ux",
          devices: zoneIds,
          children: [],
        },
      ],
    },
  ];
  panel._layoutOpen.set("area:ux", true);
  return panel._load(true);
}

async function testIdenticalZoneHidden(page) {
  const layout = async (zoneIds, zoneLabels) => {
    await page.evaluate(
      ([ids, labels]) => roomFixture(ids, labels),
      [zoneIds, zoneLabels],
    );
    return page.evaluate(() => {
      const room = panel.shadowRoot.querySelector('[data-node-id="area:ux"]');
      return {
        zone: room.querySelectorAll('[data-leaf-id="zone:ux"]').length,
        a: room.querySelectorAll('[data-placement][data-device-id="a"]').length,
        b: room.querySelectorAll('[data-placement][data-device-id="b"]').length,
      };
    });
  };
  await page.evaluate(`window.roomFixture = ${roomFixture.toString()}`);
  assert.deepEqual(
    await layout(["a", "b"]),
    { zone: 0, a: 1, b: 1 },
    "a zone with the same radars as its room is not repeated",
  );
  const note = page.locator(
    '[data-node-id="area:ux"] [data-hidden-zone="zone:ux"]',
  );
  assert.match(
    await note.innerText(),
    /Desk has the same radars as Office/,
    "the hidden zone is named so it can still be found",
  );
  await note.locator("button").click();
  assert.equal(
    await page
      .locator('[data-node-id="area:ux"] [data-leaf-id="zone:ux"]')
      .count(),
    1,
    "Show reveals the zone and its controls",
  );
  await page.evaluate(() => panel._shownZones.clear());
  assert.equal(
    (
      await layout(
        ["a", "b"],
        [{ start: 1, end: 2, state: "present", source: "manual" }],
      )
    ).zone,
    1,
    "a zone with its own labels stays visible",
  );
  assert.equal(
    (await layout(["a"])).zone,
    1,
    "a zone covering part of the room stays visible",
  );
}

async function testLoneRadarEditor(page) {
  const card = page.locator(
    '[data-node-id="area:second"] [data-placement][data-device-id="c"]',
  );
  const timeline = page.locator('[data-room-timeline="area:second"]');
  const setup = (labels) =>
    page.evaluate((labels) => {
      panel._radarEditorShown.clear();
      fixture.devices.c.history = { ...fixture.devices.c.history, labels };
      fixture.rooms.groups["area:second"] = {
        name: "Living room",
        kind: "area",
        area_id: "second",
        device_ids: ["c"],
        labels: [],
        automatic: true,
        assessment: { state: "pending" },
        presence_sources: { sources: [] },
      };
      fixture.rooms.areas.second = "Living room";
      fixture.layout.push({
        id: "area:second",
        name: "Living room",
        kind: "area",
        group_id: "area:second",
        devices: ["c"],
        children: [],
      });
      panel._layoutOpen.set("area:second", true);
      panel._groupOpen.add("area:second");
      panel._collapsed.delete(fixture.layout.at(-1).id + "/c");
      for (const key of ["history", "chart"])
        panel._setSectionCollapsed("c", key, false);
      return panel._load(true);
    }, labels);
  await page.evaluate(() => roomFixture(["a"]));
  await setup([]);
  assert.equal(await card.locator(".history-range").count(), 0);
  assert.match(
    await card.locator('.subsection[data-section="history"]').innerText(),
    /Label this radar on Living room's timeline above/,
    "a lone radar is labelled on its room",
  );
  assert.equal(
    await card.locator(".chart-home [data-chart-canvas]").count(),
    1,
    "its gate visualization stays available",
  );
  await page.evaluate(() => roomFixture(["a"]));
  await setup([{ start: 1790000000, end: 1790000600, state: "present" }]);
  const note = timeline.locator("[data-radar-labels]");
  assert.match(
    await note.innerText(),
    /1 period is labelled on Room c itself and takes priority/,
  );
  await note.locator('[data-room-action="edit-radar-labels"]').click();
  assert.equal(
    await card.locator(".history-range").count(),
    1,
    "the radar's own editor can still be opened",
  );
}

async function testSourcesBelongToGroups(page) {
  const sources = (selector) =>
    page.evaluate((selector) => {
      const root = panel.shadowRoot.querySelector(selector);
      return [...root.querySelectorAll(".presence-sources > summary")].map(
        (node) => node.childNodes[0].textContent.trim(),
      );
    }, selector);
  await page.evaluate(() => {
    delete fixture.devices.b.presence_sources;
    panel._collapsed.delete("area:ux/b");
    panel._setSectionCollapsed("b", "training", false);
    return roomFixture(["a"]);
  });
  const radar = '[data-node-id="area:ux"] [data-placement][data-device-id="b"]';
  assert.deepEqual(
    await sources(radar),
    [],
    "a radar in a room has no sources form of its own",
  );
  assert.deepEqual(
    await sources('[data-node-id="area:ux"] [data-group-id="area:ux"]'),
    ["Room presence sources"],
  );
  await page.evaluate(() => {
    fixture.devices.b.presence_sources = {
      sources: [{ entity_id: "binary_sensor.x", kind: "boolean", area: "" }],
    };
    return panel._load(true);
  });
  assert.deepEqual(
    await sources(radar),
    ["Radar presence sources (move these to its room)"],
    "existing radar sources stay editable",
  );
}

async function testCalendarRemoved(page) {
  assert.equal(
    await page.locator("#layout .history-calendar").count(),
    0,
    "History has a day picker, not a month calendar",
  );
}
