const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs/promises");

module.exports = async function testRecordedData(page, screenshotDir) {
  const card = page.locator("#recorded-data");
  assert.equal(await card.count(), 1, "raw recordings have their own card");
  assert.equal(
    await card.getAttribute("open"),
    null,
    "raw recordings start closed",
  );
  await page.evaluate(() => {
    window.rawOriginalWS = panel._hass.callWS;
    window.rawCalls = [];
    const values = Array.from({ length: 18 }, (_, i) =>
      i === 1 ? null : i === 2 ? 12.125 : (i * 10) % 100,
    );
    window.rawFixture = [
      [1792543200.125, 1792543200.175, ...values],
      [
        1792543200.175,
        1792543201,
        ...values.map((v) => (v === null ? null : v + 1)),
      ],
      [1792543205, 1792543205, ...values],
    ];
    panel._hass.callWS = async (message) => {
      if (message.type.endsWith("/recording_days")) {
        rawCalls.push(message);
        if (!message.device_id)
          return {
            devices: [
              { id: "a", name: "Radar A", days: 1, bytes: 4096 },
              { id: "archived", name: "Archived radar", days: 1, bytes: 1024 },
            ],
            storage: { recovery_bytes: 2048 },
          };
        return {
          days: [
            {
              date: "2026-10-21",
              bytes: 4096,
              day_bytes: 4096,
              log_bytes: 0,
              index_bytes: 0,
              live: false,
            },
          ],
          total: 1,
          offset: 0,
          next_offset: null,
          bytes: 4096,
        };
      }
      if (message.type.endsWith("/recording_rows")) {
        rawCalls.push(message);
        if (window.rawFailure) throw new Error("Synthetic raw read failure");
        const offset = message.offset || 0;
        const count = 2;
        return {
          date: message.date,
          keys: Array.from({ length: 9 }, (_, i) => [
            `g${i}_move`,
            `g${i}_still`,
          ]).flat(),
          rows: rawFixture.slice(offset, offset + count),
          total: 3,
          offset,
          next_offset: offset + count < 3 ? offset + count : null,
          end: 1792627200,
          revision: 7,
        };
      }
      return rawOriginalWS(message);
    };
  });
  try {
    await card.locator(":scope > summary").click();
    await card.locator('[data-raw-open="2026-10-21"]').click();
    await page.waitForFunction(
      () => panel._recordedData?.page?.rows.length === 2,
    );
    const table = card.locator("[data-raw-values]");
    assert.match(
      await table.innerText(),
      /12\.125/,
      "stored legacy fractions are not rounded",
    );
    assert.match(
      await table.innerText(),
      /00:40:00\.125/,
      "timestamps preserve milliseconds",
    );
    assert.match(await card.innerText(), /4\.00 KiB/, "day size is visible");
    assert.match(
      await card.innerText(),
      /2\.00 KiB/,
      "retained recovery files are accounted separately",
    );
    await card.locator("[data-raw-kind]").selectOption("still");
    assert.match(
      await table.innerText(),
      /Missing/,
      "unavailable gates remain missing",
    );
    await card.locator("[data-raw-next]").click();
    await page.waitForFunction(() => panel._recordedData?.page?.offset === 2);
    assert.equal(await table.locator("tbody tr").count(), 1);
    assert.match(
      await table.innerText(),
      /0\.000/,
      "zero-duration raw rows remain visible",
    );
    const calls = await page.evaluate(() => rawCalls.length);
    await page.evaluate(() => panel._load());
    assert.equal(
      await page.evaluate(() => rawCalls.length),
      calls,
      "snapshot polls do not refetch raw data",
    );
    await card.locator("[data-raw-prev]").click();
    await page.waitForFunction(() => panel._recordedData?.page?.offset === 0);
    const downloaded = page.waitForEvent("download");
    await card.locator("[data-raw-download]").click();
    const download = await downloaded;
    const content = await fs.readFile(await download.path(), "utf8");
    assert.match(content, /g8_still/);
    assert.match(content, /12\.125/);
    assert.equal(
      content.trim().split("\n").length,
      4,
      "CSV contains every row, not just current page",
    );
    assert.match(content, /2026-10-21T00:40:00\.125Z/);
    const exportCalls = await page.evaluate(() =>
      rawCalls.filter((m) => m.limit === 10000),
    );
    assert.deepEqual(
      exportCalls.map((m) => m.offset),
      [0, 2],
    );
    assert.ok(
      exportCalls.every((m) => m.end === 1792627200 && m.revision === 7),
      "CSV pages pin the same recording snapshot",
    );
    await testCancelledExport(page, card);
    await testCancelledExport(page, card, true);
    await testStaleRead(page, card);

    await page.evaluate(() => {
      window.rawFailure = true;
    });
    await card.locator("[data-raw-refresh]").click();
    await page.waitForFunction(() => !!panel._recordedData?.error);
    assert.match(await card.innerText(), /Synthetic raw read failure/);
    assert.equal(
      await table.locator("tbody tr").count(),
      2,
      "failed refresh retains prior raw rows",
    );
    const failedCalls = await page.evaluate(() => rawCalls.length);
    await page.evaluate(() => panel._load());
    assert.equal(
      await page.evaluate(() => rawCalls.length),
      failedCalls,
      "an error does not trigger an automatic retry",
    );
    await page.evaluate(() => {
      window.rawFailure = false;
    });
    await card.locator("[data-raw-refresh]").click();
    await page.waitForFunction(
      () => !panel._recordedData?.loading && !panel._recordedData?.error,
    );
    for (const width of [1440, 390]) {
      await page.setViewportSize({ width, height: 950 });
      await card.evaluate((node) => node.scrollIntoView({ block: "start" }));
      assert.equal(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
        true,
        "raw table stays inside mobile card",
      );
      await page.screenshot({
        path: path.join(screenshotDir, `recorded-data-${width}.png`),
      });
    }
  } finally {
    await page.evaluate(() => {
      panel._hass.callWS = rawOriginalWS;
    });
    await card.locator(":scope > summary").click();
    await page.setViewportSize({ width: 1400, height: 1000 });
  }
};

async function testCancelledExport(page, card, detach = false) {
  let downloads = 0;
  const countDownload = () => downloads++;
  page.on("download", countDownload);
  await page.evaluate(() => {
    window.rawRelease = null;
    window.rawBaseWS = panel._hass.callWS;
    panel._hass.callWS = async (message) => {
      if (message.type.endsWith("/recording_rows") && message.limit === 10000)
        await new Promise((resolve) => {
          window.rawRelease = resolve;
        });
      const result = await rawBaseWS(message);
      if (message.limit === 10000) {
        result.rows = rawFixture;
        result.next_offset = null;
      }
      return result;
    };
  });
  await card.locator("[data-raw-download]").click();
  await page.waitForFunction(() => !!window.rawRelease);
  if (detach) await page.evaluate(() => panel.remove());
  else await card.locator("[data-raw-cancel]").click();
  await page.evaluate(() => rawRelease());
  await page.waitForFunction(() => !panel._recordedData.export);
  if (detach) await page.evaluate(() => document.body.append(panel));
  assert.match(await card.innerText(), /Export cancelled/);
  assert.equal(downloads, 0, "cancelling must not download a partial file");
  page.off("download", countDownload);
  await page.evaluate(() => {
    panel._hass.callWS = rawBaseWS;
  });
}

async function testStaleRead(page, card) {
  await page.evaluate(() => {
    window.rawBaseWS = panel._hass.callWS;
    window.rawRelease = null;
    panel._hass.callWS = async (message) => {
      if (message.type.endsWith("/recording_rows"))
        await new Promise((resolve) => {
          window.rawRelease = resolve;
        });
      return rawBaseWS(message);
    };
  });
  await card.locator("[data-raw-refresh]").click();
  await page.waitForFunction(() => !!window.rawRelease);
  await card.locator("[data-raw-device]").selectOption("archived");
  await page.waitForFunction(
    () =>
      panel._recordedData.device === "archived" && !panel._recordedData.loading,
  );
  await page.evaluate(async () => {
    rawRelease();
    await new Promise((resolve) => setTimeout(resolve, 0));
    panel._hass.callWS = rawBaseWS;
  });
  assert.equal(
    await card.locator("[data-raw-values]").count(),
    0,
    "late readings from the previous radar must not appear under the new radar",
  );
  await card.locator('[data-raw-open="2026-10-21"]').click();
  await page.waitForFunction(() => panel._recordedData.page?.rows.length === 2);
  await card.locator("[data-raw-device]").selectOption("a");
  await card.locator('[data-raw-open="2026-10-21"]').click();
  await page.waitForFunction(() => panel._recordedData.page?.rows.length === 2);
}
