const assert = require("node:assert/strict");
const path = require("node:path");

const DESK = '[data-placement="zone:desk|door"]';
const SOFA = '[data-placement="zone:sofa|door"]';

async function bothValues(page, selector) {
  return page.evaluate(
    ([desk, sofa, field]) =>
      [desk, sofa].map(
        (card) =>
          panel.shadowRoot.querySelector(`${card} ${field}`)?.value ?? null,
      ),
    [DESK, SOFA, selector],
  );
}

async function testTree(page) {
  const layout = page.locator("#layout");
  const door = layout.locator('[data-device-id="door"]');
  assert.equal(await door.count(), 2, "door renders once per zone");
  assert.equal(
    await layout
      .locator('[data-leaf-id="zone:desk"] [data-device-id="door"]')
      .count(),
    1,
  );
  assert.equal(
    await layout
      .locator('[data-leaf-id="zone:sofa"] [data-device-id="door"]')
      .count(),
    1,
  );
  assert.deepEqual(
    await door.evaluateAll((cards) => cards.map((c) => c.dataset.placement)),
    ["zone:desk|door", "zone:sofa|door"],
  );
  assert.equal(
    await page.evaluate(() => panel._placements("door").length),
    2,
    "_placements returns every copy",
  );
  for (const id of ["a", "b", "c"])
    assert.equal(
      await layout.locator(`[data-device-id="${id}"]`).count(),
      1,
      `${id} placed once`,
    );
  const office = layout.locator('[data-node-id="area:office"]');
  assert.equal(await office.evaluate((node) => node.tagName), "DETAILS");
  assert.equal(await office.evaluate((node) => node.open), true);
  assert.match(await office.locator(":scope > summary").innerText(), /Office/);
  assert.match(
    await layout.locator('[data-leaf-id="zone:desk"] > summary h3').innerText(),
    /Desk/,
  );
  const rest = layout.locator('[data-leaf-id="area:office/rest"]');
  assert.match(
    await rest.locator(":scope > summary h3").innerText(),
    /Elsewhere in Office/,
  );
  assert.equal(
    await rest.locator('[data-device-id="c"] .not-recording').innerText(),
    "Not recording",
  );
  assert.equal(
    await layout.locator('[data-leaf-id="zone:sofa"] .not-recording').count(),
    0,
    "only listed devices are marked",
  );
  const empty = layout.locator('[data-node-id="zone:empty"]');
  assert.match(
    await empty.locator(".layout-note").innerText(),
    /No radars in this group/,
  );
  // Group headers in the tree replace the old Rooms card.
  assert.equal(await page.locator("#rooms").count(), 0);
}

async function testSharedChart(page) {
  const desk = page.locator(DESK);
  const sofa = page.locator(SOFA);
  await desk.locator(".chart-canvas svg").waitFor();
  await sofa.locator(".chart-canvas svg").waitFor();
  const before = await page.evaluate(
    () =>
      requests.filter(
        (r) =>
          r.type.endsWith("history_series_multi") && r.device_id === "door",
      ).length,
  );
  await desk.locator('[data-action="chart-range"]').selectOption("24");
  await page.waitForFunction(
    () => panel._chartState.get("door").loadedHours === 24,
  );
  assert.equal(
    await sofa.locator('[data-action="chart-range"]').inputValue(),
    "24",
    "range selection mirrors into the other copy",
  );
  const fetched = await page.evaluate(
    (count) =>
      requests
        .filter(
          (r) =>
            r.type.endsWith("history_series_multi") && r.device_id === "door",
        )
        .slice(count),
    before,
  );
  assert.deepEqual(
    fetched.map((r) => r.hours),
    [24],
    "chart data is fetched once per radar",
  );
  const hit = desk.locator('[data-role="selection-hit"]');
  await hit.scrollIntoViewIfNeeded();
  const box = await hit.boundingBox();
  const y = box.y + box.height / 2;
  await page.mouse.move(box.x + box.width * 0.3, y);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * 0.5, y, { steps: 4 });
  await page.mouse.move(box.x + box.width * 0.6, y, { steps: 4 });
  await page.mouse.up();
  await page.waitForFunction(() => !!panel._chartState.get("door").selection);
  await sofa.locator(".selection-range").waitFor();
  assert.equal(
    await sofa.locator(".selection-range").innerText(),
    await desk.locator(".selection-range").innerText(),
    "chart selection mirrors into the other copy",
  );
  await desk.locator('[data-action="selection-cancel"]').click();
  assert.equal(await sofa.locator(".selection-range").count(), 0);
}

async function testJobStatus(page) {
  await page.evaluate(() => {
    fixture.devices.door.learning_job = {
      status: "running",
      stage: "preparing",
      sources: ["user"],
      started_at: Date.now() / 1000 - 5,
    };
    return panel._load(true);
  });
  for (const selector of [DESK, SOFA])
    assert.match(
      await page.locator(`${selector} .learning-job-status`).innerText(),
      /Checking radar settings/,
    );
  await page.evaluate(() => {
    delete fixture.devices.door.learning_job;
    return panel._load(true);
  });
  for (const selector of [DESK, SOFA])
    assert.equal(
      await page.locator(`${selector} .learning-job-status`).innerText(),
      "",
    );
}

async function typeDraft(page, card, value) {
  const field = page.locator(`${card} [data-action="history-start"]`);
  await field.fill(value);
  // A poll while the field is focused is held, not applied.
  await page.evaluate(() => panel._load(true));
  assert.equal(await field.inputValue(), value);
  await field.blur();
  await page.waitForFunction(() => !panel._redrawPending);
  await page.evaluate(() => panel._load(true));
}

async function testDrafts(page) {
  await page.evaluate(() => {
    panel._setSectionCollapsed("door", "history", false);
    panel._draw();
  });
  await typeDraft(page, SOFA, "2026-09-23T12:00");
  assert.deepEqual(
    await bothValues(page, '[data-action="history-start"]'),
    ["2026-09-23T12:00", "2026-09-23T12:00"],
    "draft typed in the Sofa copy shows in both",
  );
  await typeDraft(page, DESK, "2026-09-23T13:00");
  assert.deepEqual(
    await bothValues(page, '[data-action="history-start"]'),
    ["2026-09-23T13:00", "2026-09-23T13:00"],
    "draft typed in the Desk copy is not overwritten by the later copy",
  );
}

async function testSections(page) {
  const collapsed = (card) =>
    page
      .locator(`${card} .subsection[data-section="history"]`)
      .evaluate((node) => node.classList.contains("collapsed"));
  assert.equal(await collapsed(SOFA), true, "history starts collapsed");
  await page
    .locator(`${DESK} [data-action="section-toggle"][data-section="history"]`)
    .click();
  assert.equal(await collapsed(DESK), false);
  assert.equal(await collapsed(SOFA), false, "section toggle mirrors");
  await page
    .locator(`${DESK} [data-action="section-toggle"][data-section="history"]`)
    .click();
  assert.equal(await collapsed(SOFA), true, "section collapse mirrors");
}

async function testDetailsMirror(page) {
  for (const selector of [
    '[data-detail="learning-details"]',
    ".gate-results",
    '[data-detail="evidence-review"]',
  ]) {
    await page.locator(`${DESK} ${selector} > summary`).click();
    await page.waitForFunction(
      ([card, target]) =>
        panel.shadowRoot.querySelector(`${card} ${target}`).open,
      [SOFA, selector],
    );
    await page.evaluate(() => panel._load(true));
    assert.equal(
      await page.locator(`${SOFA} ${selector}`).evaluate((node) => node.open),
      true,
      `${selector} open state mirrors into the other copy`,
    );
  }
}

async function testReviewEveryCopy(page) {
  const sofaGraph = page.locator(`${SOFA} .history-graph`);
  await page.locator(`${SOFA} [data-action="review-period"]`).click();
  await page.waitForFunction(() => !panel._chartState.get("door").loading);
  assert.deepEqual(
    await bothValues(page, '[data-action="history-start"]'),
    ["2026-09-23T11:00", "2026-09-23T11:00"],
    "reviewed range shows in every copy",
  );
  assert.deepEqual(await bothValues(page, '[data-action="history-state"]'), [
    "not_present",
    "not_present",
  ]);
  for (const card of [DESK, SOFA])
    assert.equal(
      await page
        .locator(`${card} .subsection[data-section="history"]`)
        .evaluate((node) => node.classList.contains("collapsed")),
      false,
    );
  // The clicked copy, not the first copy, is scrolled into view.
  await page.waitForFunction((card) => {
    const graph = panel.shadowRoot.querySelector(`${card} .history-graph`);
    const top = graph.getBoundingClientRect().top;
    return top >= -1 && top < 40;
  }, SOFA);
  assert.ok(await sofaGraph.isVisible());
}

async function testBusyEveryCopy(page) {
  await page.evaluate(() => {
    window.layoutBeforeBusy = panel._hass.callWS;
    panel._hass.callWS = (message) => {
      if (!message.type.endsWith("/start_learning"))
        return layoutBeforeBusy(message);
      return new Promise((resolve) => {
        window.layoutReleaseLearn = () => resolve({});
      });
    };
  });
  await page.locator(`${DESK} [data-action="learn"]`).click();
  await page.waitForFunction(() => !!window.layoutReleaseLearn);
  for (const card of [DESK, SOFA])
    assert.equal(
      await page.locator(`${card} [data-action="state"]`).isDisabled(),
      true,
      `${card} is locked while the radar is busy`,
    );
  await page.evaluate(() => {
    layoutReleaseLearn();
    delete window.layoutReleaseLearn;
    panel._hass.callWS = layoutBeforeBusy;
  });
  await page.waitForFunction(() => panel._activeActions === 0);
  for (const card of [DESK, SOFA])
    assert.equal(
      await page.locator(`${card} [data-action="state"]`).isDisabled(),
      false,
    );
}

async function doorFetches(page, revision) {
  return page.evaluate(async (next) => {
    const before = requests.length;
    fixture.devices.door.history.revision = next;
    await panel._load(true);
    await new Promise((resolve) => setTimeout(resolve, 50));
    return requests
      .slice(before)
      .filter(
        (r) =>
          r.type.endsWith("history_series_multi") && r.device_id === "door",
      ).length;
  }, revision);
}

async function testCollapseSemantics(page) {
  const collapsed = (card) =>
    page
      .locator(`${card} > .body`)
      .evaluate((node) => node.classList.contains("collapsed"));
  // A bare device id collapses every copy, and no chart is fetched.
  await page.evaluate(() => {
    panel._setSectionCollapsed("door", "chart", false);
    panel._collapsed.add("door");
    panel._draw();
  });
  assert.deepEqual(
    [await collapsed(DESK), await collapsed(SOFA)],
    [true, true],
  );
  assert.equal(await doorFetches(page, 101), 0, "no fetch when all collapsed");
  // Opening one copy is per placement and survives polling; the chart is
  // fetched because a copy is visible.
  await page.locator(`${DESK} [data-action="toggle"]`).click();
  await page.waitForFunction(() => !panel._chartState.get("door").loading);
  assert.equal(await doorFetches(page, 102), 1, "fetch while one copy open");
  assert.deepEqual(
    [await collapsed(DESK), await collapsed(SOFA)],
    [false, true],
  );
  // A bare delete opens every copy and clears per-copy overrides.
  await page.evaluate(() => {
    panel._collapsed.add("zone:desk|door");
    panel._collapsed.delete("door");
    return panel._load(true);
  });
  assert.deepEqual(
    [await collapsed(DESK), await collapsed(SOFA)],
    [false, false],
  );
}

async function testCollapse(page) {
  const collapsed = (selector) =>
    page
      .locator(`${selector} > .body`)
      .evaluate((node) => node.classList.contains("collapsed"));
  await page.locator(`${DESK} [data-action="toggle"]`).click();
  assert.equal(await collapsed(DESK), true);
  assert.equal(await collapsed(SOFA), false, "other copy stays open");
  await page.evaluate(() => panel._load(true));
  assert.equal(await collapsed(DESK), true, "collapse survives polling");
  assert.equal(await collapsed(SOFA), false);
  await page.locator(`${DESK} [data-action="toggle"]`).click();
  assert.equal(await collapsed(DESK), false);
}

async function testMobile(page, screenshotDir) {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: path.join(screenshotDir, "layout-mobile.png"),
    fullPage: true,
  });
  assert.ok(
    (await page.evaluate(() => document.documentElement.scrollWidth)) <= 390,
    "nested layout has no horizontal scroll at 390px",
  );
  await page.setViewportSize({ width: 1400, height: 1000 });
}

async function testFallback(page) {
  await page.evaluate(() => {
    delete fixture.layout;
    return panel._load(true);
  });
  const layout = page.locator("#layout");
  const leaves = layout.locator("[data-leaf-id]");
  assert.equal(await leaves.count(), 1, "one flat leaf without layout");
  assert.match(
    await leaves.locator(":scope > summary h3").innerText(),
    /All radars/,
  );
  for (const id of ["a", "b", "c", "door"])
    assert.equal(
      await leaves.locator(`[data-device-id="${id}"]`).count(),
      1,
      `${id} shown in All radars`,
    );
}

module.exports = async function testLayout(page, screenshotDir) {
  await page.evaluate(() => {
    window.layoutOriginalDevices = structuredClone(fixture.devices);
    const door = structuredClone(fixture.devices.c);
    delete door.learning_job;
    door.history = { ...(door.history || {}), labels: [], revision: 100 };
    door.last_learning = {
      ...door.last_learning,
      review: {
        period_count: 1,
        periods: [
          {
            start: Date.parse("2026-09-23T10:00:00Z") / 1000,
            end: Date.parse("2026-09-23T10:01:00Z") / 1000,
            state: "not_present",
            samples: 10,
          },
        ],
        sessions: [],
      },
    };
    fixture.devices.door = { ...door, name: "Door radar" };
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
            devices: ["a", "door"],
            children: [],
          },
          {
            id: "zone:sofa",
            kind: "zone",
            name: "Sofa",
            group_id: "zone:sofa",
            devices: ["door", "b"],
            children: [],
          },
          {
            id: "area:office/rest",
            kind: "rest",
            name: "Elsewhere in Office",
            group_id: null,
            devices: ["c"],
            not_recording: ["c"],
            children: [],
          },
        ],
      },
      {
        id: "zone:empty",
        kind: "group",
        name: "Empty",
        group_id: "zone:empty",
        devices: [],
        children: [],
        note: "No radars in this group",
      },
    ];
    return panel._load(true);
  });
  await page.evaluate(() => {
    panel._collapsed.delete("door");
    panel._draw();
  });
  await page.evaluate(() => {
    for (const node of fixture.layout) {
      panel._layoutOpen.set(node.id, true);
      for (const child of node.children || [])
        panel._layoutOpen.set(child.id, true);
    }
    for (const key of ["training", "chart", "details"])
      panel._setSectionCollapsed("door", key, false);
    panel._draw();
  });
  await testTree(page);
  await testSharedChart(page);
  await testJobStatus(page);
  await testSections(page);
  await testDetailsMirror(page);
  await testBusyEveryCopy(page);
  await testReviewEveryCopy(page);
  await testDrafts(page);
  await testCollapse(page);
  await testCollapseSemantics(page);
  await testMobile(page, screenshotDir);
  await testFallback(page);
  await page.evaluate(() => {
    fixture.devices = layoutOriginalDevices;
    return panel._load(true);
  });
};
