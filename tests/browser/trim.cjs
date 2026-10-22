const assert = require("node:assert/strict");

// Empty results are not shown, and Recommendations keeps its extras in one Details.
module.exports = async function testTrim(page) {
  await testHelpIsShort(page);
  await page.evaluate(() => {
    window.trimOriginal = structuredClone(fixture);
    for (const id of ["a", "b"]) {
      panel._collapsed.delete(id);
      panel._setSectionCollapsed(id, "details", false);
    }
    delete fixture.devices.b.last_learning;
    delete fixture.devices.b.learning_results;
    fixture.devices.b.comparison = {};
    fixture.devices.a.comparison = {
      state: "done",
      scorer_version: 1,
      patterns: { live: { score: 80 } },
      best_slots: ["live"],
    };
    return panel._load(true);
  });
  try {
    const card = (id) => page.locator(`[data-device-id="${id}"]`).first();
    const counts = (id) =>
      card(id).evaluate((node) => {
        const recommendations = node.querySelector(
          '.subsection[data-section="details"]',
        );
        return {
          comparison: node.querySelectorAll(".pattern-comparison").length,
          outcomes: node.querySelectorAll(".sample-outcome-report").length,
          header: node.querySelectorAll(".comparison-header").length,
          details: recommendations.querySelectorAll(
            ":scope details.learning-details",
          ).length,
          looseGateTable: recommendations.querySelectorAll(
            ".gate-results:not(.learning-details .gate-results)",
          ).length,
        };
      });
    assert.deepEqual(
      await counts("b"),
      {
        comparison: 0,
        outcomes: 0,
        header: 0,
        details: 1,
        looseGateTable: 0,
      },
      "a radar without results shows no empty placeholders",
    );
    const withResults = await counts("a");
    assert.equal(withResults.header, 1, "stored scores stay in the header");
    assert.equal(withResults.details, 1);
    assert.equal(
      await card("a")
        .locator("details.learning-details .pattern-comparison")
        .count(),
      1,
      "comparison sits inside Details",
    );
  } finally {
    await page.evaluate(() => {
      window.fixture = structuredClone(trimOriginal);
      return panel._load(true);
    });
  }
};

// Help is one or two lines; the README holds the detail.
async function testHelpIsShort(page) {
  const long = await page.evaluate(() => {
    const closed = [
      ...panel.shadowRoot.querySelectorAll("details:not([open])"),
    ];
    for (const details of closed) details.open = true;
    const roots = [
      "#layout",
      "#learning-schedule",
      "#timing-controls",
      "#storage-controls",
      ".layout-help",
    ].map((selector) => panel.shadowRoot.querySelector(selector));
    const found = new Map();
    for (const root of roots)
      for (const node of root.querySelectorAll("p, .muted")) {
        if (node.querySelector("p")) continue; // Judge the paragraphs themselves.
        const text = node.textContent.trim().replace(/\s+/g, " ");
        const words = text.split(" ").length;
        if (words > 45) found.set(text.slice(0, 70), words);
      }
    for (const details of closed) details.open = false;
    return [...found].map(([text, words]) => `${words}: ${text}`);
  });
  assert.deepEqual(long, [], "help paragraphs stay within 45 words");
}
