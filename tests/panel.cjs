// Run with npm test; PLAYWRIGHT_MODULE optionally selects an external installation.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs/promises");
const os = require("node:os");
const testTouchSelection = require("./browser/touch.cjs");
const testHistoryEditor = require("./browser/history.cjs");
const testApply = require("./browser/apply.cjs");
const { execFileSync } = require("node:child_process");
(async () => {
  const registration = JSON.parse(
    execFileSync(
      process.env.PYTHON || "python3",
      [path.join(__dirname, "panel_bootstrap.py")],
      { encoding: "utf8" },
    ),
  );
  const screenshotDir = await fs.mkdtemp(
    path.join(os.tmpdir(), "ld2410-panel-"),
  );
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({
      viewport: { width: 1400, height: 1000 },
      timezoneId: "Europe/London",
    });
    page.setDefaultTimeout(5000);
    const errors = [];
    page.on("pageerror", (error) => {
      errors.push(error.message);
      console.error("Browser error:", error.message);
    });
    const documentHtml =
      "<style>body{margin:0;font-family:Arial;--primary-text-color:#222;--secondary-text-color:#666;--divider-color:#ddd;--card-background-color:#fff;--secondary-background-color:#f4f4f4;--primary-color:#1976d2}</style>";
    const source =
      process.env.PANEL_SOURCE ||
      path.join(
        __dirname,
        "../custom_components/ld2410_tuner/static/ld2410-tuner-panel.js",
      );
    await page.route("http://tuner.test/**", async (route) => {
      const pathname = new URL(route.request().url()).pathname;
      if (pathname === "/")
        return route.fulfill({ contentType: "text/html", body: documentHtml });
      assert.ok(pathname.startsWith(registration.static_url + "/"));
      const filename = pathname.slice(registration.static_url.length + 1);
      const body = await fs.readFile(path.join(path.dirname(source), filename));
      return route.fulfill({ contentType: "text/javascript", body });
    });
    await page.goto("http://tuner.test/");
    await page.addScriptTag({
      // Match Home Assistant's loader choice instead of forcing module mode.
      type: registration.panel.module_url ? "module" : "text/javascript",
      url: new URL(
        registration.panel.module_url || registration.panel.js_url,
        "http://tuner.test",
      ).href,
    });
    assert.deepEqual(
      errors,
      [],
      "registered panel script must load without errors",
    );
    assert.equal(
      await page.evaluate(
        (name) => !!customElements.get(name),
        registration.panel.name,
      ),
      true,
      "Home Assistant's registered URL must define the panel element",
    );
    await page.evaluate(() => {
      window.requests = [];
      window.fail = false;
      window.race = false;
      window.fixture = {
        devices: Object.fromEntries(
          ["a", "b", "c"].map((id) => [
            id,
            {
              name: `Room ${id}`,
              sample_counts: { g0_move: { present: 100, not_present: 100 } },
              current_thresholds: { g0_move: 20 },
              last_learning: {
                method: "human_priority_v6",
                status: "ok",
                automatic_evidence: {
                  samples: { present: 10, not_present: 20 },
                  mean_confidence: { present: 0.73, not_present: 0.85 },
                  effective_weight: { present: 1.46, not_present: 3.4 },
                  deferred_samples: 0,
                },
                proposals: {
                  g0_move: {
                    threshold: 15,
                    status: "ok",
                    sensitivity: 1,
                    not_present_samples: 100,
                    noise_ceiling: 5,
                    noise_floor_p99: 5,
                  },
                },
                training: {
                  sensitivity: 1,
                  false_positive_rate: 0,
                  present_samples: 100,
                  not_present_samples: 100,
                },
                validation: {
                  sensitivity: 1,
                  false_positive_rate: 0,
                  present_samples: 20,
                  not_present_samples: 20,
                },
              },
              history: { labels: [] },
              auto_learning: {
                observations: 14,
                last: {
                  state: "present",
                  confidence: 0.73,
                  basis: "human-guided",
                },
              },
            },
          ]),
        ),
      };
      const panel = document.createElement("ld2410-tuner-panel");
      window.panel = panel;
      document.body.append(panel);
      panel.hass = {
        callWS: async (message) => {
          window.requests.push(message);
          if (message.type.endsWith("snapshot"))
            return structuredClone(window.fixture);
          if (message.type.endsWith("history_series_multi")) {
            if (window.fail) throw new Error("History offline");
            if (window.race)
              await new Promise((resolve) =>
                setTimeout(resolve, message.hours === 1 ? 150 : 10),
              );
            if (window.delay)
              await new Promise((resolve) => setTimeout(resolve, window.delay));
            const end = message.end ?? Date.now() / 1000,
              start = end - message.hours * 3600;
            return {
              start,
              end,
              bucket_seconds: message.hours <= 6 ? 60 : 300,
              labels: [],
              series: Object.fromEntries(
                message.keys.map((key) => [
                  key,
                  {
                    points: [
                      { t: start + 60, min: 5, avg: 10, max: 20 },
                      { t: end - 60, min: 6, avg: 11, max: 22 },
                    ],
                    sample_count: 2,
                  },
                ]),
              ),
            };
          }
          return {};
        },
      };
    });
    const cards = page.locator(".card");
    await cards.first().waitFor();
    assert.equal(await cards.count(), 3);
    assert.match(await cards.first().locator(".name").innerText(), /73%/);
    await cards.first().locator('[data-action="toggle"]').click();
    await cards.first().locator("svg").waitFor();
    assert.equal(
      await cards.first().locator('[data-action="chart-kind"]').inputValue(),
      "still",
    );
    // The diagnostics below use movement fixtures; select that view explicitly.
    await cards
      .first()
      .locator('[data-action="chart-kind"]')
      .selectOption("move");
    await page.waitForFunction(
      () => panel._chartState.get("a").loadedKind === "move",
    );
    for (let i = 0; i < 3; i++) {
      await cards.first().locator('[data-action="toggle"]').click();
      await cards.first().locator('[data-action="toggle"]').click();
      await page.evaluate(() => panel._load());
      assert.equal(
        await cards.count(),
        3,
        "all cards survive polling after chart render",
      );
      assert.equal(await cards.first().locator("svg").count(), 1);
    }
    await cards.nth(1).locator('[data-action="toggle"]').click();
    await cards.nth(1).locator("svg").waitFor();
    await cards
      .nth(1)
      .locator('[data-action="chart-kind"]')
      .selectOption("move");
    await page.waitForFunction(
      () => panel._chartState.get("b").loadedKind === "move",
    );
    assert.equal(await cards.count(), 3);
    await cards
      .first()
      .locator('[data-action="section-toggle"][data-section="chart"]')
      .click();
    await cards
      .first()
      .locator('[data-action="section-toggle"][data-section="chart"]')
      .click();
    assert.equal(await cards.count(), 3);

    await cards
      .first()
      .locator('[data-action="section-toggle"][data-section="history"]')
      .click();
    const from = cards.first().locator('[data-action="history-start"]');
    await from.fill("2026-09-23T12:00");
    await from.blur();
    await page.evaluate(() => panel._load());
    assert.equal(
      await from.inputValue(),
      "2026-09-23T12:00",
      "draft survives refresh",
    );

    await cards
      .first()
      .locator('[data-action="section-toggle"][data-section="auto"]')
      .click();
    assert.match(
      await cards.first().locator(".auto-head").innerText(),
      /20% × confidence/,
    );
    assert.match(
      await cards.first().locator(".auto-details").innerText(),
      /73%/,
    );
    await cards
      .first()
      .locator('[data-action="section-toggle"][data-section="details"]')
      .click();
    assert.match(
      await cards
        .first()
        .locator('[data-section="details"] .subsection-body')
        .innerText(),
      /99.9%/,
    );
    assert.match(
      await cards
        .first()
        .locator('[data-section="details"] .subsection-body')
        .innerText(),
      /Weighted automatic observations: 1.5 \/ 3.4/,
    );
    await page.evaluate(() => {
      fixture.devices.b.last_learning.status = "ok";
      fixture.devices.b.last_learning.validation = null;
      fixture.devices.b.last_learning.training = null;
      fixture.devices.b.last_learning.evidence_basis = "automatic";
      fixture.devices.b.last_learning.proposals.g0_move.evidence_basis =
        "automatic";
      return panel._load();
    });
    assert.match(
      await cards.nth(1).locator(".learn-status").innerText(),
      /estimated threshold/,
    );
    assert.equal(
      await cards.nth(1).locator('[data-action="apply"]').isDisabled(),
      false,
    );

    // Device failure counts must not be attributed to the selected clean gate.
    await page.evaluate(() => {
      window.savedLearning = structuredClone(fixture.devices.a.last_learning);
      const learning = fixture.devices.a.last_learning;
      learning.status = "unsafe";
      learning.training.false_positives = 4317;
      learning.training.not_present_samples = 5000;
      learning.proposals.g0_move = {
        threshold: 16,
        status: "unsafe",
        false_positives: 0,
        not_present_samples: 5000,
        noise_ceiling: 7,
        noise_floor_p99: 7,
        message:
          "4317 false triggers in 5000 human-labelled empty-room samples",
      };
      learning.proposals.g2_still = {
        threshold: 3,
        status: "unsafe",
        false_positives: 4317,
        not_present_samples: 5000,
      };
      learning.proposals.g2_still.exclusive_presence_samples = 4;
      learning.proposals.g2_still.weakest_exclusive_presence_energy = 5;
      learning.outlier_filter = {
        human: {
          excluded: { present: 7, not_present: 0 },
          periods: [{ start: 1700000000, end: 1700000030, samples: 5 }],
          period_count: 2,
        },
        automatic: { excluded: { present: 2, not_present: 0 } },
      };
      learning.feasibility = {
        status: "conflict",
        windows: [
          {
            scope: "all_human",
            conflict: true,
            minimum_false_samples_for_recall: 62,
            not_present_samples: 5000,
            allowed_false_samples: 25,
            allowed_missed_samples: 2,
            unavoidable_missed_samples: 4,
            unavoidable_longest_missed_run: 3,
            periods: [{ start: 1000, end: 1018, samples: 3 }],
          },
        ],
      };
      return panel._load();
    });
    let diagnostic = await cards.first().locator(".learn-status").innerText();
    assert.match(diagnostic, /This gate alone triggers on 0 \/ 5000/);
    assert.match(diagnostic, /Combined device recommendation unsafe.*4317/s);
    assert.match(diagnostic, /Gate 2 Still at 3: 4317 \/ 5000/);
    assert.match(diagnostic, /counts must not be added/);
    assert.match(
      diagnostic,
      /the retained observations cannot meet both targets with any gate-threshold combination/,
    );
    assert.match(
      diagnostic,
      /at least 62 \/ 5000 false-trigger samples; the limit is 25/,
    );
    assert.match(
      diagnostic,
      /4 labelled presence samples depend on this gate alone; weakest energy 5/,
    );
    assert.match(diagnostic, /Recorded conflicts around/);
    assert.match(
      diagnostic,
      /Excluded outliers: 7 human-labelled \/ 2 estimated presence samples/,
    );
    assert.match(
      diagnostic,
      /same retained observations determine thresholds, accuracy, episodes, feasibility and Apply eligibility/,
    );
    assert.doesNotMatch(diagnostic, /Correct labels/);
    assert.equal(
      await cards.first().locator('[data-action="apply"]').isDisabled(),
      true,
    );
    // Updated conflict periods must also invalidate an otherwise identical chart.
    await page.evaluate(() => {
      fixture.devices.a.last_learning.feasibility.windows[0].minimum_false_samples_for_recall = 63;
      return panel._load();
    });
    assert.match(
      await cards
        .first()
        .locator(".chart-canvas .evidence-conflict")
        .innerText(),
      /at least 63/,
    );
    // Changes to another gate must invalidate the chart's cached diagnostic.
    await page.evaluate(() => {
      fixture.devices.a.last_learning.proposals.g2_still.false_positives = 4000;
      return panel._load();
    });
    assert.match(
      await cards.first().locator(".learn-status").innerText(),
      /Gate 2 Still at 3: 4000 \/ 5000/,
    );
    // Exclusions remain visible on accepted candidates and update without new chart data.
    await page.evaluate(() => {
      const learning = fixture.devices.a.last_learning;
      learning.status = "ok";
      learning.feasibility.status = "not_ruled_out";
      learning.proposals.g0_move.status = "ok";
      learning.proposals.g0_move.threshold = 53;
      learning.proposals.g0_move.noise_ceiling = 22;
      learning.proposals.g0_move.separation = {
        separated: true,
        presence_reference: 84,
        preferred_threshold: 53,
      };
      return panel._load();
    });
    assert.equal(
      await cards.first().locator('[data-action="apply"]').isDisabled(),
      false,
    );
    assert.match(
      await cards.first().locator(".chart-canvas .outlier-filter").innerText(),
      /Excluded outliers: 7/,
    );
    assert.match(
      await cards.first().locator(".learn-status").innerText(),
      /presence reference.*84.*preferred threshold.*53/s,
    );
    await page.evaluate(() => {
      fixture.devices.a.last_learning.outlier_filter.human.excluded.present = 8;
      return panel._load();
    });
    assert.match(
      await cards.first().locator(".chart-canvas .outlier-filter").innerText(),
      /Excluded outliers: 8/,
    );
    await page.evaluate(() => {
      fixture.devices.a.last_learning = window.savedLearning;
      return panel._load();
    });

    // A real focused selector must render the response without needing blur.
    const range = cards.first().locator('[data-action="chart-range"]');
    const anchor = await page.evaluate(() => panel._chartState.get("a").end);
    await page.evaluate(() => {
      window.delay = 100;
      window.savedRange = panel.shadowRoot.querySelector(
        '[data-action="chart-range"]',
      );
    });
    await range.focus();
    await range.selectOption("24");
    await page.waitForFunction(
      () =>
        !panel._chartState.get("a").loading &&
        panel._chartState.get("a").loadedHours === 24,
    );
    assert.equal(
      await cards.first().locator("svg").count(),
      1,
      "focused range selector cannot strand completed chart on Loading",
    );
    assert.equal(
      await page.evaluate(
        () => panel.shadowRoot.activeElement === window.savedRange,
      ),
      true,
    );
    assert.equal(
      await page.evaluate(() => panel._chartState.get("a").end),
      anchor,
      "range changes retain the same end time",
    );
    const requestCount = await page.evaluate(
      () =>
        requests.filter((r) => r.type.endsWith("history_series_multi")).length,
    );
    await range.selectOption("6");
    assert.equal(
      await page.evaluate(() => panel._chartState.get("a").loadedHours),
      6,
    );
    assert.equal(
      await page.evaluate(
        () =>
          requests.filter((r) => r.type.endsWith("history_series_multi"))
            .length,
      ),
      requestCount,
      "switching back uses cached data",
    );
    await range.blur();
    await page.evaluate(() => {
      window.savedSvg = panel.shadowRoot.querySelector("svg");
      return panel._load();
    });
    assert.equal(
      await page.evaluate(
        () => panel.shadowRoot.querySelector("svg") === window.savedSvg,
      ),
      true,
      "unchanged chart is not rebuilt on snapshot polling",
    );
    assert.equal(
      await page.evaluate(
        () =>
          panel.shadowRoot.querySelector('[data-action="chart-range"]') ===
          window.savedRange,
      ),
      true,
      "polling preserves chart controls",
    );

    const canvasHeight = await cards
      .first()
      .locator(".chart-canvas")
      .evaluate((el) => el.getBoundingClientRect().height);
    const refresh = cards.first().locator('[data-action="chart-refresh"]');
    const buttonBefore = await refresh.evaluate((el) => ({
      x: el.getBoundingClientRect().x,
      y: el.getBoundingClientRect().y + window.scrollY,
    }));
    await refresh.click();
    assert.equal(
      await cards.first().locator("svg").count(),
      1,
      "refresh retains the existing graph while loading",
    );
    assert.equal(
      await cards
        .first()
        .locator(".chart-canvas")
        .evaluate((el) => el.getBoundingClientRect().height),
      canvasHeight,
    );
    await page.waitForFunction(() => !panel._chartState.get("a").loading);
    assert.deepEqual(
      await refresh.evaluate((el) => ({
        x: el.getBoundingClientRect().x,
        y: el.getBoundingClientRect().y + window.scrollY,
      })),
      buttonBefore,
      "refresh stays in place",
    );
    await page.evaluate(() => {
      window.delay = 0;
    });

    // Race two requests: the slow old range must not replace the selected one.
    await page.evaluate(() => {
      window.race = true;
      const cs = panel._chartState.get("a");
      cs.hours = 1;
      panel._fetchChartData("a");
      cs.hours = 24;
      panel._fetchChartData("a");
    });
    await page.waitForFunction(
      () => panel._chartState.get("a").loadedHours === 24,
    );
    await page.waitForTimeout(200);
    assert.equal(
      await page.evaluate(() => panel._chartState.get("a").loadedHours),
      24,
    );
    assert.equal(
      await cards.first().locator('[data-action="chart-range"]').inputValue(),
      "24",
      "range control matches the displayed window",
    );

    // A rendering error stays inside its own chart.
    await page.evaluate(() => {
      const cs = panel._chartState.get("a");
      cs.data.series.g0_move.points = null;
      panel._draw();
      panel._renderChartCanvas("a", true);
    });
    assert.equal(await cards.count(), 3);
    assert.match(
      await cards.first().locator(".chart-canvas").innerText(),
      /Unable to render/,
    );
    await page.evaluate(() => {
      window.race = false;
      panel._chartState.get("a").data = null;
      return panel._fetchChartData("a", true);
    });

    await page.evaluate(() => {
      window.fail = true;
      return panel._fetchChartData("a", true);
    });
    assert.match(
      await cards.first().locator(".chart-status").innerText(),
      /History offline/,
    );
    assert.equal(
      await cards.first().locator("svg").count(),
      1,
      "failed refresh retains previous graph",
    );
    await page.evaluate(() => {
      window.fail = false;
      return panel._fetchChartData("a", true);
    });
    await cards.first().locator("svg").waitFor();

    // A history timeout must settle loading and leave the previous chart usable.
    await page.evaluate(async () => {
      const original = panel._hass.callWS;
      const originalCall = panel._call;
      panel._hass.callWS = (message) =>
        message.type.endsWith("history_series_multi")
          ? new Promise(() => {})
          : original(message);
      panel._call = function (type, data, timeout) {
        return originalCall.call(
          this,
          type,
          data,
          type === "history_series_multi" ? 40 : timeout,
        );
      };
      try {
        await panel._fetchChartData("a", true);
      } finally {
        panel._hass.callWS = original;
        panel._call = originalCall;
      }
    });
    assert.equal(
      await page.evaluate(() => panel._chartState.get("a").loading),
      false,
    );
    assert.match(
      await cards.first().locator(".chart-status").innerText(),
      /timed out/,
    );
    assert.equal(await cards.first().locator("svg").count(), 1);
    await page.evaluate(() => panel._fetchChartData("a", true));

    // Initial drag rectangle, selection persistence, and cancellation cleanup.
    const hit = cards.first().locator('[data-role="selection-hit"]');
    const box = await hit.boundingBox();
    await page.mouse.move(box.x + 30, box.y + 30);
    await page.mouse.down();
    await page.mouse.move(box.x + 120, box.y + 30);
    assert.equal(await page.evaluate(() => panel._dragging), true);
    assert.equal(
      await cards.first().locator('[data-role="selection-rect"]').isVisible(),
      true,
    );
    await page.mouse.up();
    assert.equal(
      await cards.first().locator('[data-role="handle-start"]').count(),
      1,
    );
    await page.evaluate(() => panel._load());
    assert.equal(
      await cards.first().locator('[data-role="handle-start"]').count(),
      1,
    );
    await hit.dispatchEvent("pointerdown", {
      clientX: box.x + 50,
      clientY: box.y + 30,
    });
    await page.evaluate(() => window.dispatchEvent(new Event("pointercancel")));
    assert.equal(await page.evaluate(() => panel._dragging), false);

    await testApply(page);
    await testTouchSelection(page);
    await testHistoryEditor(page);
    await page.setViewportSize({ width: 390, height: 844 });
    await cards
      .first()
      .locator('.subsection[data-section="history"]')
      .screenshot({
        path: path.join(screenshotDir, "history-mobile.png"),
      });
    await page.screenshot({
      path: path.join(screenshotDir, "mobile.png"),
      fullPage: true,
    });
    assert.equal(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
      true,
      "mobile no horizontal overflow",
    );
    await page.setViewportSize({ width: 1400, height: 1000 });
    await page.screenshot({
      path: path.join(screenshotDir, "desktop.png"),
      fullPage: true,
    });
    await page.evaluate(() => {
      panel.remove();
    });
    assert.equal(await page.evaluate(() => panel._pollTimer), null);
    await page.evaluate(() => document.body.append(panel));
    assert.equal(await page.evaluate(() => Boolean(panel._pollTimer)), true);
    assert.deepEqual(errors, []);
    console.log("Screenshots:", screenshotDir);
    console.log(
      "PASS: real touch selection and resizing, canceled gestures, day navigation, saved-period editing/removal, stale saves, DST, focused range changes, stable window endpoints, cached range switching, stable refresh geometry, retained graph on refresh/error, chart controls across polling, cards, races, drafts, selection, mobile layout, reconnect; no browser errors",
    );
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
