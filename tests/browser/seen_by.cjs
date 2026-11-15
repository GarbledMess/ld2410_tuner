const assert = require("node:assert/strict");

// "Seen by" shows only inside an open room, so a snapshot asks for the rooms open now and
// the worker attributes no others.
module.exports = async function testSeenByOpenRooms(page) {
  const asked = () =>
    page.evaluate(() => {
      const snapshots = requests.filter((m) => m.type.endsWith("snapshot"));
      return snapshots[snapshots.length - 1].seen_by;
    });
  await page.evaluate(async () => {
    window.seenOpen = new Set(panel._groupOpen);
    panel._groupOpen.clear();
    await panel._load();
  });
  assert.deepEqual(await asked(), [], "no room open, none asked for");
  await page.evaluate(async () => {
    panel._groupOpen.add("area:lounge");
    panel._groupOpen.add("zone:sofa");
    await panel._load();
  });
  assert.deepEqual((await asked()).sort(), ["area:lounge", "zone:sofa"]);
  await page.evaluate(async () => {
    panel._groupOpen.clear();
    for (const id of seenOpen) panel._groupOpen.add(id);
    await panel._load();
  });
};
