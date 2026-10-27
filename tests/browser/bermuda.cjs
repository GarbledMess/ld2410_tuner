const assert = require("node:assert/strict");
const path = require("node:path");

module.exports = async function testBermuda(page, screenshotDir) {
  const section = page.locator("#bermuda");
  assert.equal(await section.getAttribute("open"), null);
  await page.evaluate(async () => {
    window.beforeBermuda = structuredClone(fixture);
    window.bermudaWS = panel._hass.callWS;
    fixture.bermuda = {
      devices: ["sensor.watch_area"],
      max_distance: null,
      rooms: {},
    };
    fixture.bermuda_devices = [
      {
        entity_id: "sensor.phone_area",
        name: "Jack’s <phone> 📱 — téléphone très long",
        area: "Bedroom <upstairs>",
      },
    ];
    fixture.rooms = {
      areas: { bedroom: "Bedroom", office: "Office" },
      members: {},
      ...fixture.rooms,
      groups: {
        "area:bedroom": {
          kind: "area",
          area_id: "bedroom",
          name: "Bedroom <upstairs> 🛏 chambre très longue",
          device_ids: ["a"],
        },
        "area:office": {
          kind: "area",
          area_id: "office",
          name: "Office",
          device_ids: ["b"],
        },
        "area:empty": {
          kind: "area",
          area_id: "empty",
          name: "Empty",
          device_ids: [],
        },
        "zone:test": {
          kind: "zone",
          area_id: "office",
          name: "Zone",
          device_ids: ["b"],
        },
      },
    };
    panel._hass.callWS = async (message) => {
      if (message.type.endsWith("/bermuda_devices"))
        return structuredClone(fixture.bermuda_devices);
      if (message.type.endsWith("/configure_bermuda")) {
        requests.push(message);
        if (window.failBermuda) throw new Error("Bermuda save failed");
        if (window.holdBermuda)
          await new Promise((resolve) => (window.releaseBermuda = resolve));
        fixture.bermuda = structuredClone(message.settings);
        return message.settings;
      }
      return bermudaWS(message);
    };
    await panel._load(true);
  });
  try {
    await section.locator(":scope > summary").click();
    assert.equal(
      await section.locator("[data-bermuda-global-range]").count(),
      1,
      "one global range replaces per-device targets",
    );
    assert.match(await section.innerText(), /never marks a room empty/i);
    assert.equal(
      await section
        .locator("[data-bermuda-radar], [data-bermuda-area]")
        .count(),
      0,
    );
    assert.equal(await section.locator("[data-bermuda-device]").count(), 2);
    assert.equal(await section.locator("[data-bermuda-room]").count(), 2);
    const phone = section.locator('[data-bermuda-device="sensor.phone_area"]');
    const watch = section.locator('[data-bermuda-device="sensor.watch_area"]');
    const office = section.locator('[data-bermuda-room="area:office"]');
    const roomRange = office.locator("[data-bermuda-range]");
    const globalRange = section.locator("[data-bermuda-global-range]");
    const save = section.locator('[data-action="bermuda-save"]');
    assert.match(
      await phone.innerText(),
      /Jack’s <phone>.*Bedroom <upstairs>/s,
    );
    assert.equal(
      await phone.getByRole("switch", { name: "Use this phone" }).count(),
      1,
    );
    assert.match(await watch.innerText(), /Unavailable/);
    await watch.getByRole("switch").uncheck();
    await phone.getByRole("switch").check();
    await office.getByRole("switch").check();
    await globalRange.fill("3");
    assert.equal(await roomRange.getAttribute("placeholder"), "Default: 3 m");
    await globalRange.fill("");
    assert.equal(await roomRange.getAttribute("placeholder"), "Any");
    await roomRange.fill("4.5");
    await page.evaluate(() => panel.shadowRoot.activeElement?.blur());
    await page.evaluate(() => panel._load(true));
    assert.equal(await roomRange.inputValue(), "4.5");
    assert.equal(await globalRange.inputValue(), "");
    await globalRange.fill("3");
    await roomRange.fill("");
    await page.evaluate(() => panel.shadowRoot.activeElement?.blur());
    await page.evaluate(() => panel._load(true));
    assert.equal(await globalRange.inputValue(), "3");
    assert.equal(await roomRange.inputValue(), "");
    assert.equal(await watch.getByRole("switch").isChecked(), false);
    assert.equal(await section.evaluate((node) => node.open), true);
    const count = () =>
      page.evaluate(
        () =>
          requests.filter((m) => m.type.endsWith("/configure_bermuda")).length,
      );
    const before = await count();
    for (const input of [globalRange, roomRange]) {
      for (const invalid of ["0", "101"]) {
        await input.fill(invalid);
        await save.click();
        assert.equal(await count(), before, "invalid range sends no request");
      }
      await input.fill(input === globalRange ? "3" : "");
    }
    await page.evaluate(() => (window.failBermuda = true));
    await save.click();
    await page.waitForFunction(() => panel._activeActions === 0);
    assert.match(
      await page.locator("#error").innerText(),
      /Bermuda save failed/,
    );
    await page.evaluate(() => panel._load(true));
    assert.equal(await globalRange.inputValue(), "3");
    assert.equal(await roomRange.inputValue(), "");
    assert.equal(await phone.getByRole("switch").isChecked(), true);
    await page.evaluate(() => {
      window.failBermuda = false;
      window.holdBermuda = true;
    });
    await save.click();
    await page.waitForFunction(() => !!window.releaseBermuda);
    assert.equal(await save.getAttribute("aria-busy"), "true");
    assert.equal(await save.isDisabled(), true);
    await page.evaluate(() => releaseBermuda());
    await page.waitForFunction(() => panel._activeActions === 0);
    assert.deepEqual(
      await page.evaluate(
        () =>
          requests.filter((m) => m.type.endsWith("/configure_bermuda")).at(-1)
            .settings,
      ),
      {
        devices: ["sensor.phone_area"],
        max_distance: 3,
        rooms: { "area:office": { max_distance: null } },
      },
    );
    assert.equal(await page.evaluate(() => panel._bermudaDraft), null);
    assert.deepEqual(
      await page.evaluate(() => [
        panel._radarSourcesShown.call({ _inAnyGroup: () => true }, "a", {
          presence_sources: { sources: [] },
        }),
        panel._radarSourcesShown.call({ _inAnyGroup: () => true }, "a", {
          presence_sources: { sources: [{ kind: "boolean" }] },
        }),
        panel._radarSourcesShown.call(
          { _inAnyGroup: () => true, _sourceDrafts: new Map([["a", {}]]) },
          "a",
          {},
        ),
      ]),
      [false, true, true],
    );
    const heading = section.locator(":scope > summary");
    await heading.focus();
    await heading.press("Enter");
    assert.equal(await section.evaluate((node) => node.open), false);
    await heading.press("Enter");
    assert.equal(await section.evaluate((node) => node.open), true);
    await heading.evaluate((node) => node.blur());
    for (const width of [390, 1440]) {
      await page.setViewportSize({ width, height: 950 });
      assert.equal(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
        true,
      );
      assert.equal(
        await section.evaluate((node) => node.scrollWidth <= node.clientWidth),
        true,
      );
      await section.screenshot({
        path: path.join(screenshotDir, `bermuda-${width}.png`),
      });
    }
    await page.evaluate(async () => {
      fixture.bermuda = { devices: [], max_distance: null, rooms: {} };
      fixture.bermuda_devices = [];
      await panel._load(true);
    });
    assert.match(await section.innerText(), /No Bermuda tracked devices found/);
    assert.equal(await section.locator("[data-bermuda-room]").count(), 2);
    await page.evaluate(async () => {
      fixture.rooms.groups = {};
      await panel._load(true);
    });
    assert.match(await section.innerText(), /No area rooms/);
  } finally {
    await page.evaluate(async () => {
      fixture = beforeBermuda;
      panel._hass.callWS = bermudaWS;
      panel._bermudaDraft = null;
      window.holdBermuda = false;
      window.failBermuda = false;
      await panel._load(true);
    });
    await section.locator(":scope > summary").click();
    await page.setViewportSize({ width: 1400, height: 1000 });
  }
};
