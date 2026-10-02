const assert = require("node:assert/strict");
const path = require("node:path");

async function tap(session, locator) {
  await locator.scrollIntoViewIfNeeded();
  const box = await locator.boundingBox();
  await session.send("Input.dispatchTouchEvent", {
    type: "touchStart",
    touchPoints: [
      { x: box.x + box.width / 2, y: box.y + box.height / 2, id: 1 },
    ],
  });
  await session.send("Input.dispatchTouchEvent", {
    type: "touchEnd",
    touchPoints: [],
  });
}

module.exports = async function testSourcePicker(page, screenshotDir) {
  await page.evaluate(() => {
    window.beforePickerStates = panel._hass.states;
    panel._hass.states = {
      ...panel._hass.states,
      "binary_sensor.porch_human": {
        state: "off",
        attributes: { friendly_name: "Front door visitor" },
      },
      "binary_sensor.offline_person": {
        state: "unavailable",
        attributes: { friendly_name: "Offline person" },
      },
      "sensor.custom_boolean": {
        state: "true",
        attributes: { friendly_name: "Independent person" },
      },
      "sensor.temperature": {
        state: "21",
        attributes: { friendly_name: "Temperature" },
      },
      "binary_sensor.escaped": {
        state: "on",
        attributes: {
          friendly_name: '<img src=x onerror="window.pickerInjected=true">',
        },
      },
      ...Object.fromEntries(
        Array.from({ length: 70 }, (_, i) => [
          `binary_sensor.test_${i}`,
          {
            state: "off",
            attributes: { friendly_name: `Test occupancy ${i}` },
          },
        ]),
      ),
    };
  });
  const form = page.locator('[data-device-id="a"] .presence-sources');
  const row = form.locator("[data-source-row]").first();
  const input = row.locator("[data-source-entity]");
  const options = row.locator("[data-source-option]");
  const popup = row.locator("[data-source-suggestions]");
  assert.equal(
    await input.getAttribute("list"),
    null,
    "Search must not depend on native datalist support",
  );
  await input.fill("FRONT visitor");
  assert.equal(
    await options.count(),
    1,
    "Match friendly names using multiple case-insensitive words",
  );
  await options.first().click();
  assert.equal(await input.inputValue(), "binary_sensor.porch_human");
  assert.equal(await popup.isVisible(), false);
  assert.equal(
    await page.evaluate(
      () => panel._sourceDrafts.get("a").sources[0].entity_id,
    ),
    "binary_sensor.porch_human",
  );

  await input.fill("camera_person");
  await input.press("ArrowDown");
  assert.ok(await input.getAttribute("aria-activedescendant"));
  await input.press("Enter");
  assert.equal(await input.inputValue(), "binary_sensor.camera_person");
  assert.equal(await input.getAttribute("aria-expanded"), "false");
  await input.fill("offline");
  assert.equal(
    await options.count(),
    1,
    "Unavailable boolean entities remain selectable",
  );
  await input.press("ArrowUp");
  assert.equal(await options.first().getAttribute("aria-selected"), "true");
  await input.press("Escape");
  assert.equal(await popup.isVisible(), false);
  await input.fill("custom_boolean");
  assert.equal(
    await options.count(),
    1,
    "Any entity with a boolean state is supported",
  );
  await input.fill("temperature");
  assert.equal(await options.count(), 0);
  assert.match(
    await row.locator("[data-source-search-status]").innerText(),
    /No matching entities/,
  );
  await input.fill("escaped");
  assert.equal(await popup.locator("img").count(), 0);
  assert.match(await options.first().innerText(), /<img/);
  assert.equal(
    await page.evaluate(() => Boolean(window.pickerInjected)),
    false,
  );
  await input.fill("");
  assert.equal(
    await options.count(),
    30,
    "Bound the DOM without excluding matches beyond the limit",
  );
  await input.fill("test_69");
  assert.equal(await options.count(), 1);

  // Same source field switches to Bermuda suggestions without mixing boolean entities.
  await row.locator("[data-source-kind]").selectOption("bermuda");
  await input.fill("phone");
  assert.equal(await options.count(), 1);
  assert.equal(
    await options.first().getAttribute("data-source-option"),
    "sensor.phone_area",
  );
  await input.fill("camera");
  assert.equal(await options.count(), 0);
  await row.locator("[data-source-kind]").selectOption("boolean");

  const viewport = page.viewportSize();
  await page.setViewportSize({ width: 390, height: 844 });
  const session = await page.context().newCDPSession(page);
  await session.send("Emulation.setTouchEmulationEnabled", { enabled: true });
  try {
    await tap(session, input);
    await input.evaluate((node) => {
      node.value = "camera";
      node.dispatchEvent(
        new CompositionEvent("compositionstart", { bubbles: true }),
      );
      node.dispatchEvent(
        new InputEvent("input", {
          bubbles: true,
          inputType: "insertCompositionText",
          data: "camera",
          isComposing: true,
        }),
      );
      node.dispatchEvent(
        new KeyboardEvent("keydown", {
          key: "Enter",
          keyCode: 229,
          isComposing: true,
          bubbles: true,
        }),
      );
    });
    assert.equal(
      await input.inputValue(),
      "camera",
      "IME Enter must not choose or discard an entity",
    );
    assert.equal(await options.count(), 1);
    await input.evaluate((node) =>
      node.dispatchEvent(
        new CompositionEvent("compositionend", {
          bubbles: true,
          data: "camera",
        }),
      ),
    );
    await page.evaluate(() => {
      window.pickerFocusedInput = panel.shadowRoot.activeElement;
      return panel._load();
    });
    assert.equal(
      await page.evaluate(
        () => panel.shadowRoot.activeElement === pickerFocusedInput,
      ),
      true,
    );
    assert.equal(await options.count(), 1, "Polling preserves search results");
    await row.locator("[data-source-picker]").screenshot({
      path: path.join(screenshotDir, "entity-picker-mobile.png"),
    });
    const box = await options.first().boundingBox();
    assert.ok(box.height >= 44 && box.x >= 0 && box.x + box.width <= 390);
    await tap(session, options.first());
    assert.equal(await input.inputValue(), "binary_sensor.camera_person");
    assert.equal(await popup.isVisible(), false);
    assert.equal(
      await page.evaluate(
        () => panel._sourceDrafts.get("a").sources[0].entity_id,
      ),
      "binary_sensor.camera_person",
    );

    // Focus inside the result list must also prevent a polling redraw during a tap.
    await input.fill("visitor");
    await options.first().focus();
    await page.evaluate(() => panel._load());
    assert.equal(await options.count(), 1);
    await tap(session, options.first());
    assert.equal(await input.inputValue(), "binary_sensor.porch_human");
    await input.fill("binary_sensor.manual_entry");
    await input.blur();
    await page.evaluate(() => panel._load());
    assert.equal(
      await input.inputValue(),
      "binary_sensor.manual_entry",
      "Direct entity IDs and drafts survive polling",
    );
  } finally {
    await session.send("Emulation.setTouchEmulationEnabled", {
      enabled: false,
    });
    await session.detach();
    await page.setViewportSize(viewport);
    await page.evaluate(() => {
      panel._hass.states = beforePickerStates;
    });
  }
};
