const assert = require("node:assert/strict");

module.exports = async function testCollapsedDefaults(page) {
  const state = await page.evaluate(() => ({
    open: [...panel.shadowRoot.querySelectorAll("details[open]")].map(
      (node) => node.querySelector("summary")?.textContent,
    ),
    expanded: [
      ...panel.shadowRoot.querySelectorAll(".subsection:not(.collapsed)"),
    ].map((node) => node.dataset.section),
    missing: [...panel.shadowRoot.querySelectorAll("summary, .subsection-head")]
      .filter(
        (node) =>
          !node.querySelector(".section-description")?.textContent.trim(),
      )
      .map((node) => node.textContent),
  }));
  assert.deepEqual(state.open, [], "every native disclosure starts closed");
  assert.deepEqual(state.expanded, [], "every device subsection starts closed");
  assert.deepEqual(
    state.missing,
    [],
    "closed headings describe their contents",
  );
  const all = page.locator('[data-leaf-id="all"]');
  await all.locator(":scope > summary").click();
  const card = all.locator('[data-device-id="a"]');
  const disclosure = card.locator('[data-action="toggle"]');
  assert.equal(await card.locator(".device-shortcuts").count(), 0);
  assert.match(await disclosure.innerText(), /Show device details/);
  assert.equal(
    await disclosure.evaluate((node) =>
      node.nextElementSibling?.classList.contains("body"),
    ),
    true,
    "device disclosure sits immediately above its content",
  );
  const controlId = await disclosure.getAttribute("aria-controls");
  assert.ok(controlId, "device disclosure identifies its content");
  assert.equal(await card.locator(".body").getAttribute("id"), controlId);
  for (const width of [1400, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    const sizes = await page.evaluate(() => {
      const root = panel.shadowRoot;
      const style = (selector) =>
        getComputedStyle(root.querySelector(selector));
      return {
        title: style("h1").fontSize,
        name: style(".name").fontSize,
        body: getComputedStyle(panel).fontSize,
        font: getComputedStyle(panel).fontFamily,
        target: root
          .querySelector('[data-action="toggle"]')
          .getBoundingClientRect().height,
      };
    });
    assert.equal(sizes.title, "20px");
    assert.equal(sizes.name, "16px");
    assert.equal(sizes.body, "14px");
    assert.match(sizes.font, /Roboto/);
    assert.ok(sizes.target >= 44, "disclosure retains a touch target");
  }
  // Wide desktops use the whole width instead of leaving empty side borders.
  await page.setViewportSize({ width: 2400, height: 1000 });
  const gutters = await page.evaluate(() => {
    const wrap = panel.shadowRoot
      .querySelector(".wrap")
      .getBoundingClientRect();
    const host = panel.getBoundingClientRect();
    return [wrap.left - host.left, host.right - wrap.right];
  });
  for (const gutter of gutters)
    assert.ok(gutter <= 16, `side gutter ${gutter}px`);
  await page.setViewportSize({ width: 1400, height: 1000 });
  await card.locator(".top").click();
  assert.equal(
    await disclosure.getAttribute("aria-expanded"),
    "false",
    "device header is not an invisible disclosure",
  );
  await disclosure.focus();
  await page.keyboard.press("Enter");
  assert.match(await disclosure.innerText(), /Hide device details/);
  await disclosure.click();
  await card.locator('[data-action="toggle"]').click();
  assert.equal(
    await card.locator(".subsection:not(.collapsed)").count(),
    0,
    "opening a card does not open its children",
  );
  const training = card.locator(
    '[data-action="section-toggle"][data-section="training"]',
  );
  assert.equal(await training.evaluate((node) => node.tagName), "BUTTON");
  assert.equal(
    await training.getAttribute("aria-controls"),
    await card
      .locator('[data-section="training"] > .subsection-body')
      .getAttribute("id"),
  );
  await training.click();
  await page.evaluate(() => panel._load(true));
  assert.equal(
    await training.getAttribute("aria-expanded"),
    "true",
    "user expansion survives polling",
  );
  await training.click();
  await card.locator('[data-action="toggle"]').click();
};
