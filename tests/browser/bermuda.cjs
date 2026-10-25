const assert = require("node:assert/strict");
const path = require("node:path");

module.exports = async function testBermuda(page, screenshotDir) {
  const section = page.locator("#bermuda");
  assert.equal(await section.count(), 1, "Bermuda has a dedicated section");
  assert.equal(
    await section.getAttribute("open"),
    null,
    "Bermuda starts closed",
  );
  await page.evaluate(async () => {
    window.beforeBermuda = structuredClone(fixture);
    window.bermudaWS = panel._hass.callWS;
    fixture.bermuda = {
      devices: { "sensor.phone_area": { radars: { a: { max_distance: 3 } } } },
    };
    fixture.bermuda_devices = [
      {
        entity_id: "sensor.phone_area",
        name: "Jack’s <phone>",
        area: "Bedroom",
      },
      { entity_id: "sensor.watch_area", name: "Watch", area: "Office" },
    ];
    fixture.rooms = {
      groups: {},
      members: {},
      ...fixture.rooms,
      areas: { bedroom: "Bedroom", office: "Office" },
    };
    fixture.rooms.groups["zone:bermuda"] = {
      name: "Both rooms",
      area_id: null,
      kind: "zone",
      device_ids: ["a", "b"],
    };
    fixture.devices.a.area_id = "bedroom";
    fixture.devices.b.area_id = "office";
    fixture.devices.c.area_id = null;
    fixture.bermuda_radars = {
      stored: { area_id: "office", reason: null },
      a: { area_id: "bedroom", reason: null },
      b: { area_id: "office", reason: null },
      c: {
        area_id: null,
        reason: "Assign this radar a Home Assistant area to use Bermuda.",
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
    assert.equal(
      await page.evaluate(() =>
        panel._radarSourcesShown.call(
          {
            _inAnyGroup: () => true,
            _data: panel._data,
          },
          "a",
          { presence_sources: { sources: [] } },
        ),
      ),
      true,
      "central assignments keep the radar confidence and trim settings accessible",
    );
    assert.deepEqual(
      await section.locator(":scope > summary").evaluate((summary) => {
        const reference = panel.shadowRoot.querySelector(
          ".global-settings-card > summary",
        );
        // Other browser tests may leave Global settings open; compare equal states.
        const wasOpen = reference.parentElement.open;
        reference.parentElement.open = summary.parentElement.open;
        const properties = ["display", "listStyleType", "minHeight", "cursor"];
        const headingProperties = [
          "display",
          "fontSize",
          "marginTop",
          "marginBottom",
        ];
        const matches = [
          properties.every(
            (key) =>
              getComputedStyle(summary)[key] ===
              getComputedStyle(reference)[key],
          ),
          headingProperties.every(
            (key) =>
              getComputedStyle(summary.querySelector("h2"))[key] ===
              getComputedStyle(reference.querySelector("h2"))[key],
          ),
        ];
        reference.parentElement.open = wasOpen;
        return matches;
      }),
      [true, true],
      "Bermuda uses the same top-level disclosure styles",
    );
    await section.locator(":scope > summary").click();
    const phone = section.locator('[data-bermuda-device="sensor.phone_area"]');
    const watch = section.locator('[data-bermuda-device="sensor.watch_area"]');
    assert.equal(await section.locator("[data-bermuda-device]").count(), 2);
    assert.match(await phone.innerText(), /Jack’s <phone>.*Bedroom/s);
    const on = (row, id) =>
      row.locator(`[data-bermuda-radar="${id}"] [data-bermuda-on]`);
    const range = (row, id) =>
      row.locator(`[data-bermuda-radar="${id}"] [data-bermuda-range]`);
    const room = phone.locator('[data-bermuda-room="zone:bermuda"]');
    assert.equal(
      await phone.locator('[data-bermuda-radar="stored"]').count(),
      1,
      "stored radars remain targets even without a visible radar card",
    );
    assert.equal(await room.count(), 1, "rooms and zones are Bermuda targets");
    assert.match(await room.innerText(), /Both rooms.*Bedroom.*Office/s);
    await room.locator("[data-bermuda-on]").check();
    await room.locator("[data-bermuda-area]").selectOption("office");
    await room.locator("[data-bermuda-range]").fill("2");
    assert.equal(await on(phone, "a").isChecked(), true);
    assert.equal(await range(phone, "a").inputValue(), "3");
    assert.equal(
      await range(watch, "a").isVisible(),
      false,
      "unchecked radar hides its range",
    );
    assert.equal(
      await range(phone, "c").isVisible(),
      false,
      "unchecked no-area radar hides its range",
    );
    assert.match(
      await on(phone, "a").locator("..").innerText(),
      /Room a · Bedroom/,
    );
    assert.match(
      await on(phone, "b").locator("..").innerText(),
      /Room b · Office/,
    );
    assert.equal(await on(phone, "c").isDisabled(), true);
    assert.match(await phone.innerText(), /Home Assistant area/);
    const noArea = phone.locator('[data-bermuda-radar="c"]');
    await noArea.locator("[data-bermuda-area]").selectOption("office");
    assert.equal(await on(phone, "c").isEnabled(), true);
    await on(phone, "c").check();
    await range(phone, "c").fill("1.5");
    await noArea.locator("[data-bermuda-area]").selectOption("");
    assert.equal(await on(phone, "c").isChecked(), false);
    assert.equal(await on(phone, "c").isDisabled(), true);
    assert.equal(await range(phone, "c").isVisible(), false);
    await noArea.locator("[data-bermuda-area]").selectOption("office");
    await on(phone, "c").check();
    await range(phone, "c").fill("1.5");
    await on(watch, "b").check();
    assert.equal(await range(watch, "b").isVisible(), true);
    assert.equal(await range(watch, "b").isDisabled(), false);
    await range(watch, "b").fill("4.5");
    await range(phone, "a").fill("");
    await page.evaluate(() => panel.shadowRoot.activeElement?.blur());
    await page.evaluate(() => panel._load(true));
    assert.equal(
      await section.evaluate((node) => node.open),
      true,
      "disclosure survives polling",
    );
    assert.equal(
      await range(watch, "b").inputValue(),
      "4.5",
      "draft survives polling",
    );
    const save = section.locator('[data-action="bermuda-save"]');
    await range(watch, "b").fill("0");
    const before = await page.evaluate(
      () =>
        requests.filter((m) => m.type.endsWith("/configure_bermuda")).length,
    );
    await save.click();
    assert.equal(
      await page.evaluate(
        () =>
          requests.filter((m) => m.type.endsWith("/configure_bermuda")).length,
      ),
      before,
      "invalid range is rejected before sending",
    );
    await range(watch, "b").fill("4.5");
    await page.evaluate(() => (window.failBermuda = true));
    await save.click();
    await page.waitForFunction(() => panel._activeActions === 0);
    assert.match(
      await page.locator("#error").innerText(),
      /Bermuda save failed/,
    );
    assert.equal(await range(watch, "b").inputValue(), "4.5");
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
      await page.evaluate(() =>
        requests.filter((m) => m.type.endsWith("/configure_bermuda")).at(-1),
      ),
      {
        type: "ld2410_tuner/configure_bermuda",
        settings: {
          devices: {
            "sensor.phone_area": {
              radars: {
                a: { max_distance: null, area: null },
                c: { max_distance: 1.5, area: "office" },
              },
              rooms: { "zone:bermuda": { max_distance: 2, area: "office" } },
            },
            "sensor.watch_area": {
              radars: { b: { max_distance: 4.5, area: null } },
              rooms: {},
            },
          },
        },
      },
    );
    await on(phone, "a").uncheck();
    await on(phone, "c").uncheck();
    assert.equal(await range(phone, "a").isVisible(), false);
    await on(phone, "a").check();
    assert.equal(await range(phone, "a").isVisible(), true);
    assert.equal(
      await range(phone, "a").inputValue(),
      "",
      "rechecking preserves a blank range",
    );
    await on(phone, "a").uncheck();
    await page.evaluate(() => (window.holdBermuda = false));
    await save.click();
    await page.waitForFunction(() => panel._activeActions === 0);
    assert.deepEqual(
      await page.evaluate(() => fixture.bermuda),
      {
        devices: {
          "sensor.phone_area": {
            radars: {},
            rooms: { "zone:bermuda": { max_distance: 2, area: "office" } },
          },
          "sensor.watch_area": {
            radars: { b: { max_distance: 4.5, area: null } },
            rooms: {},
          },
        },
      },
      "hidden range fields do not keep unchecked assignments enabled",
    );
    await page.evaluate(async () => {
      fixture.devices.b.area_id = null;
      fixture.bermuda_radars.b = {
        area_id: null,
        reason: "Assign this radar a Home Assistant area to use Bermuda.",
      };
      await panel._load(true);
    });
    assert.equal(await on(watch, "b").isChecked(), true);
    assert.equal(
      await on(watch, "b").isDisabled(),
      false,
      "saved assignment remains removable without a default area",
    );
    assert.equal(
      await range(watch, "b").isVisible(),
      true,
      "checked target retains editable range and area",
    );
    await save.click();
    await page.waitForFunction(() => panel._activeActions === 0);
    assert.deepEqual(
      await page.evaluate(
        () => fixture.bermuda.devices["sensor.watch_area"].radars.b,
      ),
      { max_distance: 4.5, area: null },
      "a hidden no-area range retains its saved assignment",
    );
    await page.evaluate(async () => {
      fixture.devices.b.area_id = "office";
      fixture.bermuda_radars.b = { area_id: "office", reason: null };
      await panel._load(true);
    });
    const heading = section.locator(":scope > summary");
    await heading.focus();
    await heading.press("Enter");
    assert.equal(
      await section.evaluate((node) => node.open),
      false,
      "keyboard collapses the native disclosure",
    );
    await heading.press("Enter");
    assert.equal(
      await section.evaluate((node) => node.open),
      true,
      "keyboard reopens the native disclosure",
    );
    await heading.evaluate((node) => node.blur());
    for (const width of [1440, 390]) {
      await page.setViewportSize({ width, height: 950 });
      await section.scrollIntoViewIfNeeded();
      for (const row of [phone, watch]) {
        for (const area of await row
          .locator(".bermuda-area:visible label")
          .all()) {
          assert.equal(
            await area.evaluate((label) => {
              const range = document.createRange();
              range.selectNodeContents(label.firstChild);
              return range.getClientRects().length;
            }),
            1,
            "Area caption stays on one line",
          );
        }
        for (const id of ["a", "b"]) {
          const toggleBox = await on(row, id).boundingBox();
          const labelBox = await on(row, id).locator("..").boundingBox();
          assert.ok(
            labelBox.height <= 48,
            `each radar label stays on one compact row: ${JSON.stringify({ width, id, labelBox, toggleBox })}`,
          );
          if (await on(row, id).isChecked()) {
            const rangeBox = await range(row, id).boundingBox();
            assert.ok(
              Math.abs(
                toggleBox.y +
                  toggleBox.height / 2 -
                  rangeBox.y -
                  rangeBox.height / 2,
              ) < 4,
              "checked range input shares the checkbox line, including on mobile",
            );
            assert.ok(rangeBox.width <= 100, "range input stays compact");
          }
        }
      }
      assert.equal(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
        true,
      );
      await section.screenshot({
        path: path.join(screenshotDir, `bermuda-${width}.png`),
      });
    }
  } finally {
    await page.evaluate(async () => {
      fixture = beforeBermuda;
      panel._hass.callWS = bermudaWS;
      panel._bermudaDraft = null;
      await panel._load(true);
    });
    await section.locator(":scope > summary").click();
    await page.setViewportSize({ width: 1400, height: 1000 });
  }
};
