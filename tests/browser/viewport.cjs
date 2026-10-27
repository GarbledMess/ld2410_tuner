const assert = require("node:assert/strict");

// A resize must wait for visible chart repaints, even while other work is held.
module.exports = async function testViewport(page) {
  const originalSize = page.viewportSize();
  await page.evaluate(() => {
    const fixture = document.createElement("div");
    fixture.style.cssText =
      "position:fixed;top:0;left:0;width:100vw;pointer-events:none";
    fixture.innerHTML = `
      <div data-chart-canvas><svg style="display:block;width:100%;height:1px" viewBox="0 0 1400 1"></svg></div>
      <div data-chart-canvas style="visibility:hidden"><svg viewBox="0 0 1 1"></svg></div>
      <div data-chart-canvas>Loading history…</div>`;
    panel.shadowRoot.append(fixture);
    const canvas = fixture.firstElementChild;
    const svg = canvas.querySelector("svg");
    window.resizeProbe = { fixture, ready: false, held: false, pending: null };
    resizeProbe.observer = new ResizeObserver(() => {
      const paint = () => {
        svg.setAttribute("viewBox", `0 0 ${canvas.clientWidth} 1`);
      };
      if (resizeProbe.held) resizeProbe.pending = paint;
      else {
        paint();
        resizeProbe.ready = true;
      }
    });
    resizeProbe.observer.observe(canvas);
  });
  await page.waitForFunction(() => resizeProbe.ready);
  await page.evaluate(() => {
    resizeProbe.held = true;
  });
  let completed = false;
  const resizing = page
    .setViewportSize({ width: 390, height: 844 })
    .then(() => {
      completed = true;
    });
  try {
    await page.waitForFunction(() => !!resizeProbe.pending);
    assert.equal(
      completed,
      false,
      "resizing waits for the visible chart's ResizeObserver repaint",
    );
    await page.evaluate(() => {
      resizeProbe.held = false;
      resizeProbe.pending();
    });
    await resizing;
    assert.equal(
      await page.evaluate(
        () => resizeProbe.fixture.querySelector("svg").viewBox.baseVal.width,
      ),
      390,
    );
  } finally {
    await page.evaluate(() => {
      resizeProbe.observer.disconnect();
      resizeProbe.fixture.remove();
      delete window.resizeProbe;
    });
    await resizing;
    await page.setViewportSize(originalSize);
  }
};
