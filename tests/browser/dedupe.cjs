const assert = require("node:assert/strict");

// Nothing on the page repeats the element directly above it.
module.exports = async function testDedupe(page) {
  await page.evaluate(() => {
    window.dedupeOriginal = structuredClone(fixture);
    panel._shownZones.clear();
  });
  try {
    await testIdenticalZoneHidden(page);
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
