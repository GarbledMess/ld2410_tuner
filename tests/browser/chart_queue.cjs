const assert = require("node:assert/strict");

module.exports = async function testChartQueue(page) {
  const result = await page.evaluate(async () => {
    const subject = document.createElement("ld2410-tuner-panel");
    subject.attachShadow({ mode: "open" });
    document.body.append(subject);
    const started = [],
      completed = [],
      gates = new Map();
    let active = 0,
      peak = 0,
      notify;
    const waitForStarts = async (count) => {
      while (started.length < count)
        await new Promise((resolve) => (notify = resolve));
    };
    subject._hass = {
      callWS: async (message) => {
        const id = message.device_id;
        active++;
        peak = Math.max(peak, active);
        started.push(id);
        const gate = new Promise((resolve) => gates.set(id, resolve));
        notify?.();
        await gate;
        active--;
        completed.push(id);
        if (id === "explicit-2") throw new Error("Expected chart failure");
        return { end: 100, series: {} };
      },
    };
    const request = (id, background, opening = false) => {
      const disclosure = document.createElement("details");
      disclosure.open = true;
      disclosure.innerHTML = `<summary>${id}</summary><div data-device-id="${id}"><div data-chart-canvas="${id}"></div></div>`;
      subject.shadowRoot.append(disclosure);
      subject._chartState.set(id, { kind: "still", hours: 6 });
      if (background || opening) {
        subject._setSectionCollapsed(id, "chart", false);
        if (opening) subject._maybeFetchChart(id, {}, false);
        else subject._maybeFetchChart(id, {});
      } else void subject._fetchChartData(id);
    };
    try {
      request("active-1", true);
      request("active-2", true);
      await waitForStarts(2);
      request("background-1", true);
      request("background-2", true);
      request("explicit-1", false, true);
      request("explicit-2", false);
      const beforeRelease = [...started];
      for (let index = 0; index < 4; index++) {
        gates.get(started[index])();
        await waitForStarts(index + 3);
      }
      gates.get(started[4])();
      gates.get(started[5])();
      // A final queued sentinel resolves after every accepted request drains.
      await subject._queueChartCall(() => null, { background: true });
      const first = {
        started: [...started],
        completed: [...completed],
        peak,
        beforeRelease,
        error: subject._chartState.get("explicit-2").error,
        loaded: [...subject._chartState]
          .filter(([, cs]) => cs.data)
          .map(([id]) => id),
      };
      // Keep a foreground backlog present while a background request waits.
      request("burst-active-1", true);
      request("burst-active-2", true);
      await waitForStarts(8);
      request("waiting-background", true);
      for (let i = 0; i < 8; i++) request(`burst-${i}`, false);
      for (let i = 6; i < 15; i++) {
        gates.get(started[i])();
        await waitForStarts(i + 3);
      }
      gates.get(started[15])();
      gates.get(started[16])();
      await subject._queueChartCall(() => null, { background: true });
      request("hidden", true);
      // Settle the first visible request before checking a later hidden revision.
      await waitForStarts(18);
      gates.get("hidden")();
      await subject._queueChartCall(() => null, { background: true });
      while (subject._chartState.get("hidden").loading) await Promise.resolve();
      subject.shadowRoot.lastElementChild.open = false;
      subject._chartState.get("hidden").end = null;
      subject._maybeFetchChart("hidden", { history: { revision: 1 } });
      await subject._queueChartCall(() => null, { background: true });
      return {
        ...first,
        burst: started.slice(8, 17),
        hiddenRequests: started.filter((id) => id === "hidden").length,
      };
    } finally {
      subject.remove();
    }
  });
  assert.deepEqual(result.beforeRelease, ["active-1", "active-2"]);
  assert.equal(result.peak, 2, "chart concurrency remains bounded at two");
  assert.deepEqual(
    result.started,
    [
      "active-1",
      "active-2",
      "explicit-1",
      "explicit-2",
      "background-1",
      "background-2",
    ],
    "explicit interactions precede pending automatic refreshes, FIFO within each class",
  );
  assert.equal(
    result.completed.length,
    6,
    "accepted work completes without cancellation or dropping background requests",
  );
  assert.equal(result.error, "Expected chart failure");
  assert.equal(
    result.hiddenRequests,
    1,
    "polling does not refresh charts whose every placement is under a closed disclosure",
  );
  assert.deepEqual(
    result.burst,
    [
      "burst-0",
      "burst-1",
      "burst-2",
      "burst-3",
      "waiting-background",
      "burst-4",
      "burst-5",
      "burst-6",
      "burst-7",
    ],
    "a waiting background refresh runs after at most four foreground dispatches even with a foreground backlog",
  );
  assert.equal(
    result.loaded.length,
    5,
    "failure does not prevent remaining background charts from loading",
  );
};
