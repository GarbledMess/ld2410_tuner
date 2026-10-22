const assert = require("node:assert/strict");

// Group headers live in the room tree: one per node or leaf with a group_id.
const header = (page, id) =>
  page.locator(`#layout [data-group-id="${id}"]`).first();

async function installFixture(page) {
  await page.evaluate(() => {
    window.beforeRooms = panel._hass.callWS;
    window.savedRooms = structuredClone(window.fixture.rooms);
    window.savedLayout = structuredClone(window.fixture.layout);
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
    // A small stand-in for the server's layout builder: zones under their
    // area, the area's remaining radars in "Elsewhere", the rest unassigned.
    window.roomsLayout = () => {
      const { areas, groups } = window.fixture.rooms;
      const placed = new Set();
      const nodes = [];
      const sorted = (items) =>
        items.sort((x, y) => x[1].name.localeCompare(y[1].name));
      for (const [areaId, name] of Object.entries(areas)) {
        const gid = Object.keys(groups).find(
          (key) =>
            groups[key].kind === "area" && groups[key].area_id === areaId,
        );
        const zones = sorted(
          Object.entries(groups).filter(
            ([, group]) => group.kind !== "area" && group.area_id === areaId,
          ),
        );
        const children = zones.map(([zid, zone]) => ({
          id: zid,
          kind: "zone",
          name: zone.name,
          group_id: zid,
          devices: [...zone.device_ids],
          children: [],
        }));
        const covered = new Set(zones.flatMap(([, zone]) => zone.device_ids));
        const rest = (groups[gid]?.device_ids || []).filter(
          (id) => !covered.has(id),
        );
        for (const id of [...covered, ...rest]) placed.add(id);
        if (!children.length)
          nodes.push({
            id: `area:${areaId}`,
            kind: "area",
            name,
            group_id: gid || null,
            devices: rest,
            children: [],
          });
        else {
          if (rest.length)
            children.push({
              id: `area:${areaId}/rest`,
              kind: "rest",
              name: `Elsewhere in ${name}`,
              group_id: null,
              devices: rest,
              children: [],
            });
          nodes.push({
            id: `area:${areaId}`,
            kind: "area",
            name,
            group_id: gid || null,
            devices: [],
            children,
          });
        }
      }
      for (const [gid, group] of sorted(
        Object.entries(groups).filter(
          ([, group]) => group.kind !== "area" && !(group.area_id in areas),
        ),
      )) {
        for (const id of group.device_ids) placed.add(id);
        nodes.push({
          id: gid,
          kind: "group",
          name: group.name,
          group_id: gid,
          devices: [...group.device_ids],
          children: [],
        });
      }
      const unplaced = Object.keys(window.fixture.devices).filter(
        (id) => !placed.has(id),
      );
      if (unplaced.length)
        nodes.push({
          id: "unassigned",
          kind: "unassigned",
          name: "Unassigned",
          group_id: null,
          devices: unplaced,
          children: [],
        });
      return nodes;
    };
    window.fixture.layout = window.roomsLayout();
    panel._hass.callWS = async (message) => {
      if (message.type.endsWith("configure_room")) {
        window.requests.push(message);
        const id = message.group_id || "zone:new";
        const { type, group_id, ...fields } = message;
        window.fixture.rooms.groups[id] = {
          ...window.fixture.rooms.groups[id],
          ...fields,
          kind: id.startsWith("area:") ? "area" : "zone",
          automatic: false,
          assessment: { state: "pending" },
        };
        window.fixture.layout = window.roomsLayout();
        return {};
      }
      if (message.type.endsWith("assess_room")) {
        window.requests.push(message);
        window.fixture.rooms.groups[message.group_id].assessment = {
          state: "running",
        };
        return { state: "running" };
      }
      if (message.type.endsWith("remove_room")) {
        window.requests.push(message);
        Object.assign(window.fixture.rooms.groups[message.group_id], {
          automatic: true,
          device_ids: ["a", "b"],
        });
        window.fixture.layout = window.roomsLayout();
        return {};
      }
      return window.beforeRooms(message);
    };
    return panel._load(true);
  });
}

async function testHeaders(page) {
  assert.equal(await page.locator("#rooms").count(), 0, "no Rooms card");
  const office = header(page, "area:office");
  const desk = header(page, "zone:desk");
  assert.equal(
    await page
      .locator(
        '[data-node-id="area:office"] > [data-group-id="area:office"] > .group-summary',
      )
      .count(),
    1,
    "the area header sits directly in the area node",
  );
  assert.equal(
    await page
      .locator(
        '[data-leaf-id="zone:desk"] > [data-group-id="zone:desk"] > .group-body',
      )
      .count(),
    1,
    "the zone header sits directly in the zone leaf",
  );
  assert.equal(
    await page
      .locator('[data-leaf-id="area:office/rest"] > [data-group-id]')
      .count(),
    0,
    "Elsewhere has no group controls",
  );
  assert.equal(
    await page.locator('[data-node-id="unassigned"] > [data-group-id]').count(),
    0,
    "Unassigned has no group controls",
  );
  assert.equal(await office.evaluate((node) => node.open), false);
  assert.equal(await desk.evaluate((node) => node.open), false);
  assert.match(
    await office.locator(".group-summary").innerText(),
    /Presence & history[\s\S]*Whole area · 2 radars[\s\S]*Not assessed/,
  );
  assert.match(
    await desk.locator(".group-summary").innerText(),
    /Presence & history[\s\S]*Zone · 1 radar[\s\S]*Not assessed$/,
  );
  assert.match(
    await office.locator(".group-summary").innerText(),
    /Desk Not assessed/,
    "area header lists its zone scores",
  );
  await page.evaluate(() => {
    fixture.rooms.groups["zone:desk"].assessment = { state: "stale" };
    return panel._load(true);
  });
  assert.equal(await desk.evaluate((node) => node.open), false);
  assert.match(
    await desk.locator(".group-summary").innerText(),
    /Needs reassessment/,
  );
  assert.equal(await desk.locator(".group-summary").isVisible(), true);
  await page.evaluate(() => {
    fixture.rooms.groups["zone:desk"].assessment = { state: "pending" };
    return panel._load(true);
  });
  await office.locator(".group-summary").click();
  await desk.locator(".group-summary").click();
  await page.evaluate(() => panel._load(true));
  assert.equal(
    await desk.evaluate((node) => node.open),
    true,
    "header open state survives polling",
  );
  await office.locator(".room-settings > summary").click();
  await desk.locator(".room-settings > summary").click();
  assert.match(
    await desk.innerText(),
    /Office · Desk radar · Manual membership/,
  );
}

async function testEditMembers(page) {
  const sofaHistory =
    '[data-device-id="b"] .subsection[data-section="history"]';
  await page.evaluate(() => {
    panel._collapsed.delete("b");
    panel._setSectionCollapsed("b", "history", true);
    panel._draw();
  });
  await page
    .locator(
      '[data-leaf-id="area:office/rest"] [data-device-id="b"] [data-action="section-toggle"][data-section="history"]',
    )
    .click();
  const desk = header(page, "zone:desk");
  await desk.locator('[data-room-action="edit"]').click();
  const form = desk.locator(".room-editor form");
  assert.equal(
    await form
      .locator('[name="room-name"]')
      .evaluate((input) => input === input.getRootNode().activeElement),
    true,
    "the editor opens inside the Desk header with focus",
  );
  assert.equal(await page.locator("#layout form").count(), 1);
  await form.locator('[data-room-member="b"]').check();
  await form.locator('[type="submit"]').click();
  await page.waitForFunction(() => !panel._roomDraft);
  const saved = await page.evaluate(() =>
    requests.findLast((r) => r.type.endsWith("configure_room")),
  );
  assert.equal(saved.group_id, "zone:desk");
  assert.deepEqual(saved.device_ids, ["a", "b"]);
  // Desk now repeats Office's radars, so it is folded into Office until shown.
  const hidden = page.locator('[data-hidden-zone="zone:desk"]');
  assert.match(await hidden.innerText(), /Desk has the same radars as Office/);
  await hidden.locator("button").click();
  assert.equal(
    await page
      .locator('[data-leaf-id="zone:desk"] [data-device-id="b"]')
      .count(),
    1,
    "saving moves the radar card into the zone",
  );
  assert.equal(
    await page.locator('[data-leaf-id="area:office/rest"]').count(),
    0,
  );
  assert.equal(
    await page
      .locator(`[data-leaf-id="zone:desk"] ${sofaHistory}`)
      .evaluate((node) => node.classList.contains("collapsed")),
    false,
    "the moved card keeps its open sections",
  );
  assert.equal(
    await page
      .locator('[data-leaf-id="zone:desk"] [data-device-id="b"] > .body')
      .evaluate((node) => node.classList.contains("collapsed")),
    false,
  );
}

async function testAddZone(page) {
  const office = header(page, "area:office");
  await page.locator('#layout [data-room-action="add-group"]').click();
  const footerForm = page.locator("#layout .layout-footer .room-editor form");
  assert.equal(
    await footerForm.locator('[name="room-area"]').inputValue(),
    "",
    "Add manual group opens an editor at the end of the layout",
  );
  assert.equal(
    await footerForm.locator("h3").innerText(),
    "Add zone or manual group",
  );
  await footerForm.locator('[data-room-action="cancel"]').click();
  await page.waitForFunction(() => !panel._roomDraft);
  assert.equal(await page.locator("#layout form").count(), 0);
  await office.locator('[data-room-action="add-zone"]').click();
  const form = office.locator(".room-editor form");
  assert.equal(
    await form.locator('[name="room-area"]').inputValue(),
    "office",
    "Add zone prefills the area",
  );
  await form.locator('[name="room-name"]').fill("Sofa <private>");
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
    requests.some(
      (r) => r.type.endsWith("configure_room") && r.name === "Sofa <private>",
    ),
  );
  const saved = await page.evaluate(() =>
    requests.findLast((r) => r.type.endsWith("configure_room")),
  );
  assert.deepEqual(saved.device_ids, ["b"]);
  assert.equal(saved.area_id, "office");
  await page.waitForFunction(() => !panel._roomDraft);
  await page.locator('[data-leaf-id="zone:new"] > summary').click();
  assert.match(
    await header(page, "zone:new").locator(".group-summary").innerText(),
    /Presence & history[\s\S]*Zone/,
  );
}

async function testAssess(page) {
  const office = header(page, "area:office");
  const desk = header(page, "zone:desk");
  await desk.locator('[data-room-action="assess"]').click();
  await page.waitForFunction(
    () => panel._data.rooms.groups["zone:desk"].assessment.state === "running",
  );
  const request = await page.evaluate(() =>
    requests.findLast((r) => r.type.endsWith("assess_room")),
  );
  assert.equal(request.group_id, "zone:desk");
  assert.equal(request.hours, 24);
  assert.match(await desk.innerText(), /You can leave and return/);
  assert.match(await desk.locator(".group-summary").innerText(), /Assessing…/);
  assert.equal(
    await desk.locator('[data-room-action="assess"]').isDisabled(),
    true,
  );
  assert.equal(
    await office.locator('[data-room-action="assess"]').isDisabled(),
    false,
  );
  const stable = await page.evaluate(async () => {
    const select = '#layout [data-group-id="zone:desk"]';
    const node = panel.shadowRoot.querySelector(select);
    const mutations = [];
    const observer = new MutationObserver((list) => mutations.push(...list));
    observer.observe(node, { childList: true, subtree: true });
    await panel._load(true);
    await panel._load(true);
    observer.disconnect();
    return {
      same: node === panel.shadowRoot.querySelector(select),
      mutations: mutations.length,
    };
  });
  assert.deepEqual(stable, { same: true, mutations: 0 });
}

async function testReport(page) {
  const office = header(page, "area:office");
  const desk = header(page, "zone:desk");
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
  assert.match(
    await office.locator(".group-summary").innerText(),
    /Desk 99.00\/100/,
  );
  assert.equal(
    await office.locator('[data-room-action="learn"]').count(),
    0,
    "whole area cannot mix independent zones while learning",
  );
}

// A one-group learning set learns and applies in its zone header.
async function testJointApply(page) {
  const desk = header(page, "zone:desk");
  await page.evaluate(() => {
    const group = window.fixture.rooms.groups["zone:desk"];
    group.device_ids = ["a", "b"];
    const learning = {
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
    window.fixture.learning_sets = {
      "zone:desk": {
        group_ids: ["zone:desk"],
        device_ids: ["a", "b"],
        learning,
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
    requests.some((r) => r.type.endsWith("apply_set")),
  );
  const joint = await page.evaluate(() =>
    requests.findLast((r) => r.type.endsWith("apply_set")),
  );
  assert.deepEqual(joint, {
    type: "ld2410_tuner/apply_set",
    set_id: "zone:desk",
    result_id: "joint-result",
    source: "user",
  });
  await page.evaluate(() => {
    delete window.fixture.learning_sets;
    return panel._load(true);
  });
}

async function testOpenJointResult(page) {
  await page.evaluate(() => {
    panel._groupOpen.delete("zone:desk");
    panel._layoutOpen.set("zone:desk", false);
    panel._draw();
    panel._openJointResult({ group_id: "zone:desk" });
  });
  assert.equal(
    await page
      .locator('[data-leaf-id="zone:desk"]')
      .evaluate((node) => node.open),
    true,
  );
  assert.equal(
    await header(page, "zone:desk").evaluate((node) => node.open),
    true,
    "a joint result opens its group header in the tree",
  );
}

async function testRestoreDefault(page) {
  const office = header(page, "area:office");
  assert.equal(
    await office.locator('[data-room-action="remove"]').count(),
    0,
    "automatic area membership has nothing to restore",
  );
  await page.evaluate(() => {
    fixture.rooms.groups["area:office"].automatic = false;
    fixture.rooms.groups["area:office"].device_ids = ["a"];
    return panel._load(true);
  });
  const restore = office.locator('[data-room-action="remove"]');
  assert.equal(await restore.innerText(), "Restore area default");
  await restore.click();
  await page.waitForFunction(() =>
    requests.some((r) => r.type.endsWith("remove_room")),
  );
  const removed = await page.evaluate(() =>
    requests.findLast((r) => r.type.endsWith("remove_room")),
  );
  assert.deepEqual(removed, {
    type: "ld2410_tuner/remove_room",
    group_id: "area:office",
  });
  await page.waitForFunction(
    () =>
      !panel.shadowRoot.querySelector(
        '[data-group-id="area:office"] [data-room-action="remove"]',
      ),
  );
}

async function testMobile(page) {
  await page.setViewportSize({ width: 412, height: 850 });
  assert.equal(
    await header(page, "zone:new").locator(".group-summary").isVisible(),
    true,
  );
  await page.evaluate(() => {
    panel._groupOpen.add("zone:new");
    panel._draw();
  });
  await header(page, "area:office")
    .locator('[data-room-action="edit"]')
    .click();
  await page.setViewportSize({ width: 390, height: 844 });
  assert.ok(
    (await page.evaluate(() => document.documentElement.scrollWidth)) <= 390,
    "open group headers and editor have no horizontal scroll at 390px",
  );
  await header(page, "area:office")
    .locator('[data-room-action="cancel"]')
    .click();
}

async function testNoAreas(page) {
  await page.evaluate(() => {
    fixture.rooms = { areas: {}, members: {}, groups: {} };
    fixture.layout = roomsLayout();
    return panel._load(true);
  });
  assert.match(
    await page.locator("#layout .layout-footer").innerText(),
    /Assign recording radars to Home Assistant areas, or create a group below\./,
  );
  assert.equal(await page.locator("#layout [data-group-id]").count(), 0);
  const help = page.locator("details.layout-help");
  assert.equal(await help.count(), 1);
  assert.equal(await help.evaluate((node) => node.open), false);
  assert.match(
    await help.locator("summary").innerText(),
    /How rooms and zones work/,
  );
  assert.match(
    await help.evaluate((node) => node.textContent),
    /Assess complementary radar coverage/,
  );
  // Learning is described by learning sets, not by "the most specific group".
  const text = await help.evaluate((node) => node.textContent);
  assert.match(
    text,
    /Linked zones learn together, so a shared radar gets one value that serves every zone it covers/,
  );
  assert.match(
    text,
    /Device Learn and overnight learning resolve a radar to its learning set\./,
  );
  assert.doesNotMatch(
    text,
    /most specific unambiguous group|independent zones/,
  );
}

// A click on a node or leaf summary is recorded at once; the toggle event is
// queued, so a poll right after the click must not undo it.
async function testTreeToggleRace(page) {
  const leaf = page.locator('#layout [data-leaf-id="zone:desk"]');
  for (const open of [false, true]) {
    await leaf.locator(":scope > summary h3").click();
    await page.evaluate(() => panel._load(true));
    assert.equal(
      await leaf.evaluate((node) => node.open),
      open,
      "leaf open state set by a click survives an immediate poll",
    );
  }
}

async function testManualAndRemovedArea(page) {
  await page.evaluate(() => {
    fixture.rooms.groups["zone:manual"] = {
      name: "Hallway",
      kind: "zone",
      area_id: null,
      device_ids: ["c"],
      automatic: false,
      assessment: { state: "pending" },
    };
    fixture.rooms.groups["area:gone"] = {
      name: "Gone",
      kind: "area",
      area_id: "gone",
      device_ids: ["a"],
      automatic: true,
      assessment: { state: "pending" },
    };
    fixture.layout = [
      ...roomsLayout(),
      {
        id: "area:gone",
        kind: "area",
        name: "Gone",
        group_id: "area:gone",
        devices: ["a"],
        children: [],
      },
    ];
    panel._groupOpen.add("zone:manual");
    panel._groupOpen.add("area:gone");
    panel._layoutOpen.set("zone:manual", true);
    panel._layoutOpen.set("area:gone", true);
    return panel._load(true);
  });
  const manual = header(page, "zone:manual");
  await manual.locator(".room-settings > summary").click();
  assert.match(
    await manual.locator(".group-summary").innerText(),
    /Presence & history[\s\S]*Manual group · 1 radar[\s\S]*Not assessed$/,
  );
  assert.match(
    await manual.locator(".room-settings > p").first().innerText(),
    /^Manual group ·/,
  );
  const gone = header(page, "area:gone");
  await gone.locator(".room-settings > summary").click();
  assert.match(await gone.locator(".group-summary").innerText(), /Whole area/);
  assert.match(
    await gone.locator(".room-settings > p").first().innerText(),
    /^Area removed ·/,
  );
  assert.equal(
    await header(page, "area:office")
      .locator('[data-room-action="add-zone"]')
      .count(),
    1,
  );
  assert.equal(
    await gone.locator('[data-room-action="add-zone"]').count(),
    0,
    "no Add zone for an area that no longer exists",
  );
  await page.evaluate(() => {
    delete fixture.rooms.groups["zone:manual"];
    delete fixture.rooms.groups["area:gone"];
    fixture.layout = roomsLayout();
    return panel._load(true);
  });
}

async function testJointResultFromCard(page) {
  const apply = '#layout [data-device-id="a"] [data-action="apply"]';
  const clickApply = () =>
    page.evaluate((selector) => {
      panel.shadowRoot.querySelector(selector).click();
      return new Promise((resolve) => setTimeout(resolve, 50));
    }, apply);
  await page.evaluate(() => {
    window.roomsSavedLearning = structuredClone(
      fixture.devices.a.last_learning,
    );
    fixture.devices.a.last_learning.joint = {
      group_id: "zone:desk",
      name: "Desk",
      report: { room: { score: null, reason: "Not enough evidence" } },
    };
    panel._groupOpen.delete("zone:desk");
    panel._draw();
    return panel._load(true);
  });
  await clickApply();
  assert.equal(
    await header(page, "zone:desk").evaluate((node) => node.open),
    true,
    "a device's joint result opens its group header",
  );
  await page.evaluate(() => {
    fixture.devices.a.last_learning.joint.group_id = "zone:gone";
    return panel._load(true);
  });
  await clickApply();
  assert.match(
    await page.locator("#error").innerText(),
    /This joint group was removed\. Configure its members and learn again\./,
  );
  await page.evaluate(() => {
    fixture.devices.a.last_learning = window.roomsSavedLearning;
    panel._showError("");
    return panel._load(true);
  });
}

async function testEditorFallsBackToFooter(page) {
  await header(page, "zone:new").locator(".room-settings > summary").click();
  await header(page, "zone:new").locator('[data-room-action="edit"]').click();
  const name = header(page, "zone:new").locator('[name="room-name"]');
  await name.fill("Sofa renamed");
  await name.blur();
  await page.evaluate(() => {
    delete fixture.rooms.groups["zone:new"];
    fixture.layout = roomsLayout();
    return panel._load(true);
  });
  assert.equal(await header(page, "zone:new").count(), 0);
  const form = page.locator("#layout .layout-footer .room-editor form");
  assert.equal(
    await form.locator('[name="room-name"]').inputValue(),
    "Sofa renamed",
    "the draft moves to the footer when its group disappears",
  );
  assert.match(await form.locator("h3").innerText(), /Edit group/);
  await form.locator('[data-room-action="cancel"]').click();
  assert.equal(await page.locator("#layout .room-editor").count(), 0);
  assert.equal(await page.evaluate(() => panel._roomEditorAt ?? null), null);
}

async function testZeroDevices(page) {
  await page.evaluate(() => {
    window.roomsSavedDevices = fixture.devices;
    fixture.devices = {};
    fixture.layout = [];
    return panel._load(true);
  });
  const layout = page.locator("#layout");
  assert.match(
    await layout.innerText(),
    /No LD2410 gate energy entities were discovered\./,
  );
  const footer = layout.locator(".layout-footer");
  assert.match(
    await footer.innerText(),
    /Assign recording radars to Home Assistant areas, or create a group below\./,
  );
  await footer.locator('[data-room-action="add-group"]').click();
  assert.equal(
    await footer.locator('.room-editor [name="room-name"]').count(),
    1,
    "groups can still be added without radars",
  );
  await footer.locator('[data-room-action="cancel"]').click();
  await page.evaluate(() => {
    fixture.devices = window.roomsSavedDevices;
    fixture.layout = roomsLayout();
    return panel._load(true);
  });
}

// ---- Learning sets (spec 5) ---------------------------------------------
// Desk [a, b] and Sofa [b, c] share the Door radar (b), so they learn as one
// linked set. Garage (d) is in no group and keeps its own Learn/Apply.
const LINKED = "zone:desk+zone:sofa";
const band = (page, id = LINKED) =>
  page.locator(`#layout [data-set-id="${id}"]`);
const card = (page, id) => page.locator(`#layout [data-device-id="${id}"]`);

async function installLearningSets(page) {
  await page.evaluate((LINKED) => {
    window.setsSavedDevices = structuredClone(fixture.devices);
    fixture.devices.d = {
      ...structuredClone(fixture.devices.c),
      name: "Garage radar",
    };
    for (const id of ["a", "b", "c"])
      fixture.devices[id].current_thresholds = { g0_move: 20 };
    const zone = (name, area_id, device_ids, extra = {}) => ({
      name,
      kind: "zone",
      area_id,
      device_ids,
      automatic: false,
      assessment: { state: "pending" },
      ...extra,
    });
    fixture.rooms = {
      areas: { office: "Office", hall: "Hall" },
      members: {
        a: { name: "Desk radar", area_id: "office", recording_enabled: true },
        b: { name: "Door radar", area_id: "office", recording_enabled: true },
        c: { name: "Sofa radar", area_id: "office", recording_enabled: true },
        d: { name: "Garage radar", area_id: null, recording_enabled: true },
      },
      groups: {
        "area:office": {
          name: "Office",
          kind: "area",
          area_id: "office",
          device_ids: ["a", "b", "c"],
          automatic: true,
          assessment: { state: "pending" },
        },
        // A one-group entry left over from before Desk joined the set.
        "zone:desk": zone("Desk", "office", ["a", "b"], {
          learning: {
            job: { status: "error", error: "Stale desk failure" },
            slots: {
              user: {
                id: "stale-desk",
                status: "ok",
                before: { room: { score: 11 } },
                after: { room: { score: 12 } },
                thresholds: { a: { g0_move: 99 } },
                signature: { a: { thresholds: { g0_move: 20 } } },
              },
            },
          },
        }),
        "zone:sofa": zone("Sofa", "office", ["b", "c"]),
      },
    };
    const zoneReport = (before, after, costBefore, costAfter) => ({
      before: { room: { score: before } },
      after: { room: { score: after } },
      cost_before: costBefore,
      cost_after: costAfter,
    });
    window.linkedLearning = {
      job: {
        status: "completed",
        group_id: LINKED,
        group_ids: ["zone:desk", "zone:sofa"],
      },
      slots: {
        user: {
          id: "linked-result",
          method: "room_joint_v1",
          status: "ok",
          group_id: LINKED,
          group_ids: ["zone:desk", "zone:sofa"],
          before: { room: null, devices: {} },
          after: { room: null, devices: {} },
          cost_before: 9,
          cost_after: 6.5,
          groups: {
            "zone:desk": zoneReport(50, 60, 5, 3),
            "zone:sofa": zoneReport(70, 75, 4, 3.5),
          },
          thresholds: { a: { g0_move: 30 }, b: { g0_move: 25 } },
          signature: {
            a: { thresholds: { g0_move: 20 } },
            b: { thresholds: { g0_move: 20 } },
            c: { thresholds: { g0_move: 20 } },
          },
        },
      },
    };
    fixture.learning_sets = {
      [LINKED]: {
        group_ids: ["zone:desk", "zone:sofa"],
        device_ids: ["a", "b", "c"],
        learning: structuredClone(window.linkedLearning),
      },
    };
    fixture.layout = roomsLayout();
    for (const id of ["a", "b", "c", "d"]) {
      panel._collapsed.delete(id);
      panel._setSectionCollapsed(id, "details", false);
    }
    return panel._load(true);
  }, LINKED);
}

async function testLinkedBand(page) {
  const linked = band(page);
  assert.equal(
    await page
      .locator(`[data-node-id="area:office"] > [data-set-id="${LINKED}"]`)
      .count(),
    1,
    "a set within one area renders its band inside that area",
  );
  const text = await linked.innerText();
  assert.match(text, /Linked: Desk \+ Sofa · share Door radar/);
  assert.match(text, /Desk: 50\.00\/100 → 60\.00\/100/);
  assert.match(text, /Sofa: 70\.00\/100 → 75\.00\/100/);
  assert.match(text, /Total error cost: 9\.000 → 6\.500/);
  assert.doesNotMatch(
    text,
    /Stored zone score|Combined score/,
    "a linked set has no single room score",
  );
  assert.match(text, /Desk radar.*20 → 30/);
  assert.match(text, /Door radar.*20 → 25/);
  assert.equal(
    await linked.locator('[data-room-action="learn"]').innerText(),
    "Learn linked zones",
  );
  await linked.locator('[data-room-action="learn"]').click();
  await page.waitForFunction(() =>
    requests.some((r) => r.type.endsWith("learn_set")),
  );
  assert.deepEqual(
    await page.evaluate(() =>
      requests.findLast((r) => r.type.endsWith("learn_set")),
    ),
    { type: "ld2410_tuner/learn_set", set_id: LINKED },
  );
  const apply = linked.locator('[data-room-action="apply-joint"]');
  assert.match(await apply.innerText(), /^Review & apply/);
  const applySets = await page.evaluate(
    () => requests.filter((r) => r.type.endsWith("apply_set")).length,
  );
  page.once("dialog", (dialog) => dialog.accept());
  await apply.click();
  await page.waitForFunction(
    (before) =>
      requests.filter((r) => r.type.endsWith("apply_set")).length > before,
    applySets,
  );
  assert.deepEqual(
    await page.evaluate(() =>
      requests.findLast((r) => r.type.endsWith("apply_set")),
    ),
    {
      type: "ld2410_tuner/apply_set",
      set_id: LINKED,
      result_id: "linked-result",
      source: "user",
    },
  );
}

async function testLinkedAutomaticApply(page) {
  await page.evaluate((LINKED) => {
    fixture.learning_sets[LINKED].learning.automatic_apply = {
      status: "skipped",
      source: "user",
      reason: "A zone would get worse",
      assessment: {
        patterns: { live: { error_cost: 9 }, user: { error_cost: 8 } },
        zones: {
          "zone:desk": {
            live: { score: 50, error_cost: 5 },
            user: { score: 40, error_cost: 6 },
          },
          "zone:sofa": {
            live: { score: 70, error_cost: 4 },
            user: { score: 90, error_cost: 2 },
          },
        },
      },
    };
    return panel._load(true);
  }, LINKED);
  const text = await band(page).locator(".automatic-apply-report").innerText();
  assert.match(text, /A zone would get worse/);
  assert.match(text, /total error cost 9\.000 → 8\.000/);
  assert.match(text, /Desk 50\.00\/100 → 40\.00\/100/);
  assert.match(text, /Sofa 70\.00\/100 → 90\.00\/100/);
  assert.doesNotMatch(text, /unscored/);
}

async function testZoneHeadersUseTheSet(page) {
  const desk = header(page, "zone:desk");
  await page.evaluate(() => {
    panel._groupOpen.add("zone:desk");
    panel._groupOpen.add("zone:sofa");
    panel._layoutOpen.set("zone:sofa", true);
    panel._groupOpen.add("area:office");
    return panel._load(true);
  });
  const office = await header(page, "area:office")
    .locator(".group-body")
    .innerText();
  assert.match(
    office,
    /Zones that share a radar learn together in their Linked band\./,
  );
  assert.doesNotMatch(office, /Learn each zone below in its own header/);
  const body = await desk.locator(".group-body").innerText();
  assert.match(body, /Linked: Desk \+ Sofa/);
  assert.match(body, /This zone: 50\.00\/100 → 60\.00\/100/);
  assert.doesNotMatch(
    body,
    /99|11\.00|Stale desk failure/,
    "a zone in a linked set never shows its stale one-group entry",
  );
  assert.equal(
    await desk.locator('[data-room-action="learn"]').count(),
    0,
    "zones of a linked set learn from the band",
  );
  assert.equal(
    await desk.locator('[data-room-action="apply-joint"]').count(),
    0,
  );
  assert.match(
    await header(page, "zone:sofa").locator(".group-body").innerText(),
    /This zone: 70\.00\/100 → 75\.00\/100/,
  );
  await page.evaluate((LINKED) => {
    fixture.learning_sets[LINKED].learning.job = { status: "running" };
    return panel._load(true);
  }, LINKED);
  assert.match(
    await desk.locator(".group-body").innerText(),
    /Linked learning in progress/,
  );
  assert.equal(
    await band(page).locator('[data-room-action="learn"]').isDisabled(),
    true,
  );
  await page.evaluate((LINKED) => {
    fixture.learning_sets[LINKED].learning.job = { status: "completed" };
    panel._layoutOpen.set("area:office", false);
    return panel._load(true);
  }, LINKED);
  await desk
    .locator('[data-room-action="show-set"]')
    .evaluate((button) => button.click());
  assert.equal(
    await page
      .locator('[data-node-id="area:office"]')
      .evaluate((node) => node.open),
    true,
    "the zone's link reveals the band",
  );
}

async function testGroupedCards(page) {
  for (const id of ["a", "b", "c"]) {
    const copy = card(page, id).first();
    assert.equal(await copy.locator('[data-action="learn"]').count(), 0, id);
    assert.equal(await copy.locator('[data-action="apply"]').count(), 0, id);
    assert.match(
      await copy.locator(".learned-with").innerText(),
      /Thresholds: learned with Desk \+ Sofa ↑/,
    );
  }
  const desk = card(page, "a").first().locator(".learned-with");
  assert.match(await desk.innerText(), /g0_move: current 20 · proposed 30/);
  assert.match(
    await card(page, "c").first().locator(".learned-with").innerText(),
    /No threshold changes proposed for this radar\./,
  );
  // The old per-radar joint block points at an Apply button grouped cards
  // no longer have, and would show one zone's score under the set's name.
  await page.evaluate((LINKED) => {
    const joint = {
      group_id: LINKED,
      name: "Desk + Sofa",
      report: { room: { score: 60 } },
    };
    fixture.devices.a.last_learning.joint = joint;
    fixture.devices.d.last_learning.joint = joint;
    panel._collapsed.delete("d");
    panel._setSectionCollapsed("d", "details", false);
    return panel._load(true);
  }, LINKED);
  assert.doesNotMatch(
    await card(page, "a").first().innerText(),
    /Joint result|Use the Apply button/,
    "grouped cards drop the old joint-result block",
  );
  assert.match(
    await card(page, "d").innerText(),
    /Joint result · Desk \+ Sofa/,
    "an ungrouped card keeps its saved joint-result block",
  );
  await page.evaluate(() => {
    delete fixture.devices.a.last_learning.joint;
    delete fixture.devices.d.last_learning.joint;
    return panel._load(true);
  });
  assert.doesNotMatch(await desk.innerText(), /result is stale/);
  const garage = card(page, "d");
  assert.equal(await garage.locator('[data-action="learn"]').count(), 1);
  assert.equal(await garage.locator('[data-action="apply"]').count(), 1);
  assert.equal(await garage.locator(".learned-with").count(), 0);
  // A manual gate edit (outside the set's Apply) makes the set's result stale.
  await page.evaluate(() => {
    fixture.devices.a.current_thresholds = { g0_move: 27 };
    fixture.devices.a.last_applied = { g0_move: 18 };
    return panel._load(true);
  });
  const edited = await desk.innerText();
  assert.match(edited, /g0_move: current 27 · proposed 30 · last applied 18/);
  assert.match(
    edited,
    /Manual gate edit: Desk \+ Sofa's result is stale\. Learn the set again\./,
  );
  assert.doesNotMatch(
    await card(page, "b").first().locator(".learned-with").innerText(),
    /result is stale/,
  );
  // After the set's own Apply the radar matches the proposal: not stale.
  await page.evaluate(() => {
    fixture.devices.a.current_thresholds = { g0_move: 30 };
    return panel._load(true);
  });
  assert.doesNotMatch(await desk.innerText(), /result is stale/);
  await page.evaluate(() => {
    fixture.devices.a.last_applied = {};
    return panel._load(true);
  });
  await testManualEditAcrossSlots(page, desk);
  await page.evaluate(() => {
    panel._layoutOpen.set("area:office", false);
    return panel._load(true);
  });
  await card(page, "a")
    .first()
    .locator('[data-action="show-set"]')
    .evaluate((button) => button.click());
  assert.equal(
    await page
      .locator('[data-node-id="area:office"]')
      .evaluate((node) => node.open),
    true,
    "↑ reveals the set's band",
  );
}

// The panel views one slot, but the radar may hold another slot's values
// (an overnight automatic Apply) or the set's last applied ones.
async function testManualEditAcrossSlots(page, desk) {
  const stale = /result is stale/;
  const set = (current, applied = {}) =>
    page.evaluate(
      ([LINKED, current, applied]) => {
        const learning = fixture.learning_sets[LINKED].learning;
        learning.slots.automatic = {
          ...structuredClone(learning.slots.user),
          id: "linked-overnight",
          thresholds: { a: { g0_move: 24 } },
        };
        fixture.devices.a.current_thresholds = { g0_move: current };
        fixture.devices.a.last_applied = applied;
        return panel._load(true);
      },
      [LINKED, current, applied],
    );
  await set(24);
  assert.match(
    await desk.innerText(),
    /proposed 30/,
    "the user slot is the one viewed",
  );
  assert.doesNotMatch(
    await desk.innerText(),
    stale,
    "the other slot's automatically applied value is not a manual edit",
  );
  await set(22, { g0_move: 22 });
  assert.doesNotMatch(
    await desk.innerText(),
    stale,
    "the last applied value is not a manual edit",
  );
  await set(27, { g0_move: 22 });
  assert.match(await desk.innerText(), stale);
  await page.evaluate((LINKED) => {
    delete fixture.learning_sets[LINKED].learning.slots.automatic;
    fixture.devices.a.current_thresholds = { g0_move: 30 };
    fixture.devices.a.last_applied = {};
    return panel._load(true);
  }, LINKED);
}

// fit_set skips zones without usable labels; a zone can also be missing
// from an older result, and a cost can be absent.
async function testLinkedSkippedZones(page) {
  await page.evaluate((LINKED) => {
    const result = fixture.learning_sets[LINKED].learning.slots.user;
    delete result.groups["zone:desk"];
    result.groups["zone:sofa"] = { skipped: "No room labels" };
    result.cost_after = null;
    return panel._load(true);
  }, LINKED);
  const text = await band(page).innerText();
  assert.match(text, /Desk: not in this result/);
  assert.match(text, /Sofa: skipped: No room labels/);
  assert.match(text, /Total error cost: 9\.000 → — /);
  await page.evaluate((LINKED) => {
    fixture.learning_sets[LINKED].learning.slots.user = structuredClone(
      window.linkedLearning.slots.user,
    );
    return panel._load(true);
  }, LINKED);
}

async function testNoSetResult(page) {
  await page.evaluate((LINKED) => {
    window.setLearningBefore = fixture.learning_sets[LINKED].learning;
    fixture.learning_sets[LINKED].learning = null;
    return panel._load(true);
  }, LINKED);
  assert.match(
    await card(page, "a").first().locator(".learned-with").innerText(),
    /No set result yet\./,
  );
  assert.doesNotMatch(
    await header(page, "zone:desk").locator(".group-body").innerText(),
    /This zone:/,
  );
  await page.evaluate((LINKED) => {
    fixture.learning_sets[LINKED].learning = window.setLearningBefore;
    return panel._load(true);
  }, LINKED);
}

// Revealing a set that no longer exists, or whose band is not drawn (no
// radars discovered), does nothing rather than failing.
async function testRevealSetEdges(page) {
  const errors = await page.evaluate(async (LINKED) => {
    const found = [];
    const attempt = (id) => {
      try {
        panel._revealSet(id);
      } catch (error) {
        found.push(error.message);
      }
    };
    attempt("zone:gone+zone:missing");
    window.revealDevices = fixture.devices;
    fixture.devices = {};
    await panel._load(true);
    attempt(LINKED);
    fixture.devices = window.revealDevices;
    await panel._load(true);
    return found;
  }, LINKED);
  assert.deepEqual(errors, []);
  assert.equal(await band(page).count(), 1);
}

async function testSetSpanningAreas(page) {
  await page.evaluate(() => {
    fixture.rooms.groups["zone:sofa"].area_id = "hall";
    fixture.layout = roomsLayout();
    return panel._load(true);
  });
  assert.equal(
    await page.locator(`#layout > [data-set-id="${LINKED}"]`).count(),
    1,
    "a set spanning areas renders its band at the top level",
  );
  assert.equal(
    await page.locator(`[data-node-id] [data-set-id="${LINKED}"]`).count(),
    0,
  );
  await page.evaluate(() => {
    fixture.rooms.groups["zone:sofa"].area_id = "office";
    fixture.layout = roomsLayout();
    return panel._load(true);
  });
}

async function testOneGroupSet(page) {
  await page.evaluate(() => {
    fixture.rooms.groups["zone:sofa"].device_ids = ["c"];
    fixture.layout = roomsLayout();
    fixture.learning_sets = {
      "zone:desk": {
        group_ids: ["zone:desk"],
        device_ids: ["a", "b"],
        learning: {
          slots: {
            user: {
              id: "desk-result",
              method: "room_joint_v1",
              status: "ok",
              group_id: "zone:desk",
              before: { room: { score: 50 } },
              after: { room: { score: 60 } },
              thresholds: { a: { g0_move: 30 } },
              signature: { a: { thresholds: { g0_move: 20 } } },
            },
          },
        },
      },
    };
    fixture.devices.a.current_thresholds = { g0_move: 20 };
    return panel._load(true);
  });
  assert.equal(await band(page).count(), 0, "no band for a one-group set");
  const desk = header(page, "zone:desk");
  const learn = desk.locator('[data-room-action="learn"]');
  assert.match(await learn.innerText(), /^Learn together/);
  assert.match(
    await desk.locator('[data-room-action="apply-joint"]').innerText(),
    /^Review & apply/,
  );
  const body = await desk.locator(".group-body").innerText();
  assert.match(body, /50\.00\/100 → 60\.00\/100/);
  assert.doesNotMatch(body, /→ 99/);
  await page.evaluate(() => {
    panel._groupOpen.add("area:office");
    return panel._load(true);
  });
  assert.match(
    await header(page, "area:office").locator(".group-body").innerText(),
    /Learn each zone below in its own header; whole-area learning would mix their occupancy\./,
    "with no linked zones the area points to its zones",
  );
  await learn.click();
  await page.waitForFunction(() =>
    requests.some(
      (r) => r.type.endsWith("learn_set") && r.set_id === "zone:desk",
    ),
  );
  assert.match(
    await card(page, "a").first().locator(".learned-with").innerText(),
    /Thresholds: learned with Desk ↑/,
  );
  assert.equal(
    await card(page, "c").first().locator('[data-action="learn"]').count(),
    1,
    "a single-radar zone's radar learns individually",
  );
  assert.equal(
    await header(page, "zone:sofa")
      .locator('[data-room-action="learn"]')
      .count(),
    0,
  );
}

// One scoreboard: the room score heads each group, a card under a group shows
// its role in that group, and the individual score moves into a detail.
const copyOf = (page, leaf, id) =>
  page.locator(`#layout [data-placement="${leaf}|${id}"]`);

async function installScoreboard(page) {
  await page.evaluate(() => {
    const role = (fields) => ({
      score: 12,
      exclusive_presence_seconds: 0,
      occupied_seconds: 0,
      empty_seconds: 0,
      false_active_seconds: 0,
      missed_seen_seconds: 0,
      missed_seen_inferred_seconds: 0,
      missed_seen_automatic_seconds: 0,
      ...fields,
    });
    const report = (score, devices) => ({
      state: "ready",
      start: 1000,
      end: 4600,
      scorer_version: 1,
      room: { score, basis: "human", outcomes: {} },
      devices,
    });
    const { groups } = fixture.rooms;
    groups["area:office"].assessment = report(90, {});
    groups["zone:desk"].assessment = report(88, {
      // A report from before per-member roles were recorded.
      a: { score: 12, exclusive_presence_seconds: 0 },
      b: role({
        exclusive_presence_seconds: 710,
        occupied_seconds: 1000,
        empty_seconds: 1000,
        false_active_seconds: 4,
        missed_seen_seconds: 130,
        missed_seen_inferred_seconds: 40,
        missed_seen_automatic_seconds: 60,
      }),
    });
    groups["zone:sofa"].assessment = report(77, {
      b: role({
        exclusive_presence_seconds: 100,
        occupied_seconds: 500,
        empty_seconds: 200,
        false_active_seconds: 50,
      }),
      c: role({
        exclusive_presence_seconds: 4000,
        occupied_seconds: 4000,
        missed_seen_seconds: 3720,
        missed_seen_inferred_seconds: 0.4,
        missed_seen_automatic_seconds: 3600,
      }),
    });
    fixture.devices.b.comparison = {
      state: "ready",
      patterns: { live: { score: 93 } },
      best_slots: ["live"],
    };
    return panel._load(true);
  });
}

async function testScoreboard(page) {
  await page.locator('[data-leaf-id="zone:sofa"] > summary').click();
  assert.match(
    await copyOf(page, "zone:desk", "b").locator(".top").innerText(),
    /Desk · Not assessed yet/,
  );
  await installScoreboard(page);
  const desk = copyOf(page, "zone:desk", "b");
  const sofa = copyOf(page, "zone:sofa", "b");
  const deskTop = await desk.locator(".top").innerText();
  assert.match(deskTop, /Covers alone 71%/);
  assert.match(deskTop, /False-active 0\.4%/);
  assert.match(
    deskTop,
    /Missed what it saw 2m 10s \(incl\. 40s inferred, 1m automatic\)/,
  );
  assert.doesNotMatch(deskTop, /Current|Best/, "no individual headline");
  const sofaTop = await sofa.locator(".top").innerText();
  assert.match(sofaTop, /Covers alone 20%/);
  assert.match(sofaTop, /False-active 25%/);
  assert.match(sofaTop, /Missed what it saw 0s(?! \()/);
  assert.match(
    await copyOf(page, "zone:desk", "a").locator(".top").innerText(),
    /Desk · Covers alone — · False-active — · Missed what it saw —/,
    "a report without role fields shows —",
  );
  assert.match(
    await copyOf(page, "zone:sofa", "c").locator(".top").innerText(),
    /Covers alone 100% · False-active — · Missed what it saw 1h 2m \(incl\. <1s inferred, 1h automatic\)/,
    "no empty time shows — instead of dividing by zero",
  );
  const own = desk.locator('details[data-detail="own-score"]');
  assert.equal(
    await own.locator("summary").innerText(),
    "Radar on its own\nIndividual performance using this radar's own labels.",
  );
  const collapsed = () =>
    desk.locator(".body").evaluate((body) => body.matches(".collapsed"));
  const before = await collapsed();
  await own.locator("summary").click();
  assert.equal(await own.evaluate((node) => node.open), true);
  assert.equal(
    await collapsed(),
    before,
    "opening the detail does not toggle the card",
  );
  const ownText = await own.innerText();
  assert.match(ownText, /Current 93\.00\/100 · Best 93\.00\/100/);
  assert.match(ownText, /different measure.*not comparable/i);
  const garage = card(page, "d").locator(".top");
  assert.match(
    await garage.locator('[data-action="review-comparison"]').innerText(),
    /^Current — · Best —$/,
    "the ungrouped card keeps its headline",
  );
  assert.equal(
    await card(page, "d").locator(".room-role, .own-score").count(),
    0,
  );
  assert.match(
    await header(page, "area:office").locator(".group-summary").innerText(),
    /Whole area[\s\S]*90\.00\/100[\s\S]*Zones: Desk 88\.00\/100, Sofa 77\.00\/100/,
  );
  assert.match(
    await band(page).innerText(),
    /Zone scores: Desk 88\.00\/100 · Sofa 77\.00\/100/,
  );
  await page.setViewportSize({ width: 390, height: 844 });
  assert.ok(
    (await page.evaluate(() => document.documentElement.scrollWidth)) <= 390,
    "role lines and the own-score detail fit 390px",
  );
  await page.setViewportSize({ width: 1400, height: 1000 });
  await page.evaluate(() => {
    for (const group of Object.values(fixture.rooms.groups))
      group.assessment = { state: "pending" };
    delete fixture.devices.b.comparison;
    return panel._load(true);
  });
}

// Membership decides the role line: a leaf's fallback radars (recording
// paused, so not in the area group) keep the individual headline, and
// "Elsewhere" radars show their role in the parent area's group.
async function testScoreboardMembership(page) {
  await page.evaluate(() => {
    window.scoreboardSaved = structuredClone(fixture.rooms);
    const role = (fields) => ({
      occupied_seconds: 100,
      empty_seconds: 100,
      exclusive_presence_seconds: 0,
      false_active_seconds: 0,
      missed_seen_seconds: 0,
      ...fields,
    });
    const report = (devices) => ({
      state: "ready",
      room: { score: 80, outcomes: {} },
      devices,
    });
    const { groups, members } = fixture.rooms;
    members.d.area_id = "hall";
    groups["area:hall"] = {
      name: "Hall",
      kind: "area",
      area_id: "hall",
      device_ids: ["c"],
      automatic: true,
      assessment: report({
        c: role({
          exclusive_presence_seconds: 99.96,
          false_active_seconds: 0.05,
        }),
      }),
    };
    groups["area:office"].device_ids = ["a", "b"];
    groups["area:office"].assessment = report({
      b: role({ exclusive_presence_seconds: 9.97, false_active_seconds: 0.1 }),
    });
    groups["zone:desk"].device_ids = ["a"];
    // Server shape: a zone-less area holds its members plus fallback radars
    // flagged not_recording; "Elsewhere" has no group of its own.
    fixture.layout = [
      {
        id: "area:hall",
        kind: "area",
        name: "Hall",
        group_id: "area:hall",
        devices: ["c", "d"],
        not_recording: ["d"],
        children: [],
      },
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
            devices: ["b", "d"],
            not_recording: ["d"],
            children: [],
          },
        ],
      },
    ];
    return panel._load(true);
  });
  await page.locator('[data-node-id="area:hall"] > summary').click();
  const hallMember = await copyOf(page, "area:hall", "c")
    .locator(".top")
    .innerText();
  assert.match(
    hallMember,
    /Hall · Covers alone 99\.9% · False-active <0\.1%/,
    "just under 100% never shows 100%; tiny non-zero shows <0.1%",
  );
  for (const leaf of ["area:hall", "area:office/rest"]) {
    const paused = copyOf(page, leaf, "d");
    assert.match(
      await paused.locator('[data-action="review-comparison"]').innerText(),
      /^Current — · Best —$/,
      `${leaf}: a fallback radar outside the group keeps its headline`,
    );
    assert.match(await paused.locator(".top").innerText(), /Not recording/);
    assert.equal(
      await paused.locator(".room-role, .own-score").count(),
      0,
      leaf,
    );
  }
  assert.match(
    await copyOf(page, "area:office/rest", "b").locator(".top").innerText(),
    /Office · Covers alone 10% · False-active 0\.1% · Missed what it saw 0s/,
    "an Elsewhere radar shows its role in the area group, at one precision",
  );
  assert.equal(
    await page
      .locator('[data-leaf-id="area:office/rest"] > [data-group-id]')
      .count(),
    0,
    "Elsewhere keeps a plain heading",
  );
  await page.evaluate(() => {
    fixture.rooms = window.scoreboardSaved;
    fixture.layout = roomsLayout();
    return panel._load(true);
  });
}

// A card without the individual-score button must not abort the draw.
async function testDrawWithoutComparisonButton(page) {
  const drawn = await page.evaluate(async () => {
    panel._comparisonHeaderHtml = () => "";
    panel.shadowRoot.querySelector("#layout").replaceChildren();
    try {
      await panel._load(true);
      const layout = panel.shadowRoot.querySelector("#layout");
      return {
        cards: layout.querySelectorAll("[data-placement]").length,
        footer: layout.querySelectorAll("[data-layout-footer]").length,
      };
    } finally {
      delete panel._comparisonHeaderHtml;
      await panel._load(true);
    }
  });
  assert.equal(drawn.footer, 1, "the layout draw completes");
  assert.ok(drawn.cards > 0, "cards are drawn");
}

async function testLearningSetsMobile(page) {
  await page.evaluate((LINKED) => {
    fixture.learning_sets = {
      [LINKED]: {
        group_ids: ["zone:desk", "zone:sofa"],
        device_ids: ["a", "b", "c"],
        learning: structuredClone(window.linkedLearning),
      },
    };
    fixture.rooms.groups["zone:sofa"].device_ids = ["b", "c"];
    fixture.layout = roomsLayout();
    panel._groupOpen.add("zone:desk");
    panel._layoutOpen.set("area:office", true);
    return panel._load(true);
  }, LINKED);
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await band(page).isVisible(), true);
  assert.ok(
    (await page.evaluate(() => document.documentElement.scrollWidth)) <= 390,
    "the linked band, zone headers and grouped cards fit 390px",
  );
  await page.setViewportSize({ width: 1400, height: 1000 });
}

async function testLearningSets(page) {
  await installLearningSets(page);
  await testScoreboard(page);
  await testScoreboardMembership(page);
  await testDrawWithoutComparisonButton(page);
  await testLinkedBand(page);
  await testLinkedAutomaticApply(page);
  await testLinkedSkippedZones(page);
  await testZoneHeadersUseTheSet(page);
  await testNoSetResult(page);
  await testGroupedCards(page);
  await testRevealSetEdges(page);
  await testSetSpanningAreas(page);
  await testOneGroupSet(page);
  await testLearningSetsMobile(page);
  await page.evaluate(() => {
    delete fixture.learning_sets;
    fixture.devices = window.setsSavedDevices;
    fixture.rooms = { areas: {}, members: {}, groups: {} };
    fixture.layout = roomsLayout();
    return panel._load(true);
  });
}

module.exports = async function testRooms(page) {
  await installFixture(page);
  await page.evaluate(() => {
    for (const node of fixture.layout) {
      panel._layoutOpen.set(node.id, true);
      for (const child of node.children || [])
        panel._layoutOpen.set(child.id, true);
    }
    panel._draw();
  });
  await testHeaders(page);
  await testTreeToggleRace(page);
  await testEditMembers(page);
  await testAddZone(page);
  await testAssess(page);
  await testReport(page);
  await testJointApply(page);
  await testOpenJointResult(page);
  await testRestoreDefault(page);
  await testManualAndRemovedArea(page);
  await testJointResultFromCard(page);
  await testMobile(page);
  await testEditorFallsBackToFooter(page);
  await testNoAreas(page);
  await testZeroDevices(page);
  await testLearningSets(page);
  await page.evaluate(() => {
    panel._hass.callWS = window.beforeRooms;
    window.fixture.rooms = window.savedRooms;
    if (window.savedLayout === undefined) delete window.fixture.layout;
    else window.fixture.layout = window.savedLayout;
    panel._roomDraft = null;
    return panel._load(true);
  });
  await page.setViewportSize({ width: 1400, height: 1000 });
};
