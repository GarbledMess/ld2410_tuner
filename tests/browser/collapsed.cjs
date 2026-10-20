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
  await card.locator('[data-action="toggle"]').click();
  assert.equal(
    await card.locator(".subsection:not(.collapsed)").count(),
    0,
    "opening a card does not open its children",
  );
  const training = card.locator(
    '[data-action="section-toggle"][data-section="training"]',
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
