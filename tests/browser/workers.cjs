const assert = require("node:assert/strict");

module.exports = async function testWorkers(page) {
  const card = page.locator("#workers");
  await page.evaluate(() => {
    panel._data.workers = {
      calculations: { active: null, foreground: 0, background: 0 },
      display: { active: null, foreground: 0, background: 0 },
      storage: { active: null, foreground: 0, background: 0 },
    };
    panel._drawWorkers();
  });
  assert.match(await card.innerText(), /Main worker.*Idle/s);

  // Snapshot polls must update this read-only card while another panel action
  // defers redraws of editable controls.
  await page.evaluate(async () => {
    fixture.workers = structuredClone(panel._data.workers);
    fixture.workers.calculations.active = {
      operation: "learn",
      stage: "running",
      running_seconds: 4,
      eta_seconds: null,
      eta_state: "estimating",
      progress_percent: null,
      resources: null,
    };
    const original = panel._isEditing;
    panel._isEditing = () => true;
    try {
      await panel._load(true);
    } finally {
      panel._isEditing = original;
      delete fixture.workers;
    }
  });
  assert.match(await card.innerText(), /Learn/);

  await page.evaluate(() => {
    panel._data.workers.calculations = {
      active: {
        operation: "learn",
        stage: "running",
        running_seconds: 15,
        eta_seconds: 45,
        eta_state: "estimated",
        progress_percent: null,
        resources: { rss_kib: 8192, cpu_seconds: 3 },
      },
      foreground: 2,
      background: 1,
    };
    panel._drawWorkers();
  });
  const text = await card.innerText();
  assert.match(text, /Learn/);
  assert.match(text, /45s estimated remaining/);
  assert.match(text, /3 queued/);
  assert.match(text, /Progress unavailable/);
  assert.match(text, /8 MiB/);
  assert.equal(await card.locator("progress").getAttribute("value"), null);

  await page.evaluate(() => {
    Object.assign(panel._data.workers.calculations.active, {
      eta_seconds: null,
      eta_state: "past_estimate",
      progress_percent: 40,
    });
    panel._drawWorkers();
  });
  assert.match(await card.innerText(), /Past estimate/);
  assert.match(await card.innerText(), /40%/);
  assert.equal(await card.locator("progress").getAttribute("value"), "40");
};
