import {
  CHART_W,
  CHART_H,
  CHART_MARGIN,
  CHART_PLOT_H,
  GATE_COLORS,
} from "./constants.js";

// Reduce raw gates immediately: room caches retain one signal series per radar,
// not eighteen copies of the original history response.
function strongest(data) {
  const points = new Map();
  for (const series of Object.values(data.series || {}))
    for (const point of series.points || []) {
      const value = point.max ?? point.avg;
      if (!Number.isFinite(value) || !Number.isFinite(point.t)) continue;
      const previous = points.get(point.t);
      if (!previous || previous.avg < value)
        points.set(point.t, { ...point, avg: value });
    }
  return [...points.values()].sort((a, b) => a.t - b.t);
}

export const panelRoomGraph = {
  _roomGraphState(gid) {
    this._roomCharts ||= new Map();
    if (!this._roomCharts.has(gid))
      this._roomCharts.set(gid, { kind: "all", request: 0, cache: new Map() });
    return this._roomCharts.get(gid);
  },

  _roomGraphHtml(gid) {
    const state = this._roomGraphState(gid);
    const options = [
      ["all", "All signals"],
      ["move", "Movement"],
      ["still", "Still presence"],
    ]
      .map(
        ([value, label]) =>
          `<option value="${value}" ${state.kind === value ? "selected" : ""}>${label}</option>`,
      )
      .join("");
    return `<div class="room-graph"><div class="chart-controls"><label>Signals<select data-room-graph-kind>${options}</select></label><button type="button" data-room-graph-refresh>Refresh graph</button></div><div class="room-graph-status muted" data-preserve-children role="status" aria-live="polite"></div><div class="room-signal-canvas" data-room-graph="${this._esc(gid)}" data-chart-canvas="room:${this._esc(gid)}"></div><div class="room-graph-legend" data-preserve-children aria-label="Radars in this graph"></div><p class="muted">Strongest signal from each radar (0–100). Signals help you review a period; they do not confirm that someone was there.</p></div>`;
  },

  _wireRoomGraph(section, gid, group) {
    const state = this._roomGraphState(gid);
    section.querySelector("[data-room-graph-kind]").onchange = (event) => {
      state.kind = event.target.value;
      this._loadRoomGraph(gid, group);
    };
    section.querySelector("[data-room-graph-refresh]").onclick = () =>
      this._loadRoomGraph(gid, group, true);
    if (this._groupOpen.has(gid)) {
      const canvas = section.querySelector("[data-room-graph]");
      if (state.element !== canvas) {
        state.observer?.disconnect();
        state.observer = new ResizeObserver(() => this._renderRoomGraph(gid));
        state.observer.observe(canvas);
        state.element = canvas;
      }
      this._renderRoomGraph(gid);
      this._loadRoomGraph(gid, group);
    }
  },

  _roomGraphSection(gid) {
    return this.shadowRoot.querySelector(
      `[data-room-timeline="${CSS.escape(gid)}"]`,
    );
  },

  async _loadRoomGraph(gid, group, refresh = false) {
    const state = this._roomGraphState(gid);
    const day = this._historyStateFor(`room:${gid}`).day;
    const bounds = this._historyDayBounds(day);
    const currentDay =
      Date.now() / 1000 >= bounds.start && Date.now() / 1000 < bounds.end;
    const ids = group.device_ids.filter((id) => this._data.devices[id]);
    const signature = JSON.stringify([
      day,
      state.kind,
      ids.map((id) => [id, this._data.devices[id].history?.revision || 0]),
    ]);
    if (
      !refresh &&
      (state.pendingKey === signature ||
        (!currentDay && state.loadedKey === signature))
    )
      return;
    // New live readings must not turn each snapshot poll into another chart job.
    if (
      !refresh &&
      state.loadedDay === day &&
      state.loadedKind === state.kind &&
      JSON.stringify(state.data?.series.map((item) => item.id)) ===
        JSON.stringify(ids) &&
      Date.now() - (state.loadedAt || 0) < 15000
    )
      return;
    const request = ++state.request;
    state.pendingKey = signature;
    state.loading = true;
    state.error = null;
    state.day = day;
    if (refresh) state.cache.clear();
    if (state.cache.has(signature) && !refresh && !currentDay) {
      state.data = state.cache.get(signature);
      this._finishRoomGraph(state, signature, day);
      this._renderRoomGraph(gid);
      return;
    }
    this._renderRoomGraph(gid);
    const end = Math.min(bounds.end, Date.now() / 1000);
    try {
      const series =
        end <= bounds.start
          ? []
          : await Promise.all(
              ids.map(async (id, index) => {
                const kind = state.kind;
                const keys = Array.from({ length: 9 }, (_, gate) =>
                  ["move", "still"]
                    .filter((item) => kind === "all" || item === kind)
                    .map((item) => `g${gate}_${item}`),
                ).flat();
                const data = await this._queueChartCall(() => {
                  if (!this.isConnected || request !== state.request)
                    return null;
                  return this._chartCall({
                    device_id: id,
                    keys,
                    hours: Math.max(0.1, (end - bounds.start) / 3600),
                    end,
                  });
                });
                return (
                  data && {
                    id,
                    name: this._data.devices[id]?.name || "Unavailable radar",
                    color: GATE_COLORS[index % GATE_COLORS.length],
                    points: strongest(data),
                    bucket: data.bucket_seconds,
                  }
                );
              }),
            );
      if (request !== state.request) return;
      state.data = { bounds, series: series.filter(Boolean) };
      state.cache.set(signature, state.data);
      while (state.cache.size > 3)
        state.cache.delete(state.cache.keys().next().value);
      this._finishRoomGraph(state, signature, day);
    } catch (error) {
      if (request === state.request)
        state.error = error.message || String(error);
    } finally {
      if (request === state.request) {
        state.loading = false;
        state.pendingKey = null;
        this._renderRoomGraph(gid);
      }
    }
  },

  _finishRoomGraph(state, signature, day) {
    state.version = (state.version || 0) + 1;
    state.loadedKey = signature;
    state.loadedDay = day;
    state.loadedKind = state.kind;
    state.loadedAt = Date.now();
    state.loading = false;
    state.pendingKey = null;
  },

  _renderRoomGraph(gid) {
    const section = this._roomGraphSection(gid);
    if (
      !section ||
      !this.isConnected ||
      !this._groupOpen.has(gid) ||
      this._dragging
    )
      return;
    const state = this._roomGraphState(gid);
    const canvas = section.querySelector("[data-room-graph]");
    const status = section.querySelector(".room-graph-status");
    const statusText = state.loading
      ? "Loading recorded signals. Waiting for the tuner if it is busy…"
      : state.error
        ? `Graph unavailable: ${state.error} You can retry with Refresh graph.`
        : "Drag across the graph or the label bar to select a period.";
    if (status.textContent !== statusText) status.textContent = statusText;
    status.classList.toggle("is-busy", !!state.loading);
    if (
      !state.data ||
      state.loadedDay !== this._historyStateFor(`room:${gid}`).day
    ) {
      if (!state.loading)
        canvas.innerHTML =
          '<p class="muted">No recorded signals for this day.</p>';
      else if (
        state.loadedDay !== this._historyStateFor(`room:${gid}`).day ||
        !canvas.querySelector("svg")
      )
        canvas.innerHTML = '<p class="muted">Loading signals…</p>';
      return;
    }
    const width = Math.max(
      320,
      Math.min(CHART_W, canvas.clientWidth || CHART_W),
    );
    const labels = this._roomsData().groups[gid]?.labels || [];
    const paintKey = JSON.stringify([state.version, width, labels]);
    const repaint = canvas.dataset.paintKey !== paintKey;
    if (repaint) {
      canvas.innerHTML = this._roomGraphSvg(gid, state.data, width);
      canvas.dataset.paintKey = paintKey;
    }
    const plotRatio = canvas.clientWidth / width;
    for (const element of section.querySelectorAll(
      ".history-timeline-editor > .history-range, .history-timeline-editor > .history-axis",
    )) {
      element.style.marginLeft = `${CHART_MARGIN.left * plotRatio}px`;
      element.style.marginRight = `${CHART_MARGIN.right * plotRatio}px`;
    }
    const legend = section.querySelector(".room-graph-legend");
    if (repaint)
      legend.innerHTML = state.data.series
        .map(
          (item) =>
            `<button type="button" data-room-device="${this._esc(item.id)}"><i style="background:${item.color}"></i>${this._esc(item.name)}<span>View radar</span></button>`,
        )
        .join("");
    for (const button of legend.querySelectorAll("[data-room-device]"))
      button.onclick = () => {
        const owner = section.closest("[data-node-id], [data-leaf-id]");
        const card = owner.querySelector(
          `[data-device-id="${CSS.escape(button.dataset.roomDevice)}"]`,
        );
        if (card)
          this._openDeviceSection(card, button.dataset.roomDevice, "history");
      };
    canvas.querySelector("[data-room-plot-hit]").onpointerdown = (event) =>
      this._dragHistoryRange(event, section, `room:${gid}`);
    this._paintHistoryRange(section, `room:${gid}`);
  },

  _roomGraphSvg(gid, data, width) {
    const { bounds, series } = data;
    const plotWidth = width - CHART_MARGIN.left - CHART_MARGIN.right;
    const x = (time) =>
      CHART_MARGIN.left +
      ((time - bounds.start) / (bounds.end - bounds.start)) * plotWidth;
    const y = (value) =>
      CHART_MARGIN.top +
      (1 - Math.max(0, Math.min(100, value)) / 100) * CHART_PLOT_H;
    const grid = [0, 50, 100]
      .map(
        (value) =>
          `<line class="grid-line" x1="${CHART_MARGIN.left}" x2="${width - CHART_MARGIN.right}" y1="${y(value)}" y2="${y(value)}"/><text class="axis-label" x="${CHART_MARGIN.left - 5}" y="${y(value) + 3}" text-anchor="end">${value}</text>`,
      )
      .join("");
    const ticks = Array.from({ length: 5 }, (_, i) => {
      const time = bounds.start + ((bounds.end - bounds.start) * i) / 4;
      const label = new Date(time * 1000).toLocaleTimeString(undefined, {
        hour: "2-digit",
        minute: "2-digit",
        hourCycle: "h23",
      });
      return `<text class="axis-label" x="${x(time)}" y="${CHART_H - 6}" text-anchor="${this._tickAnchor(i, 5)}"><title>${this._esc(this._historyTime(time))}</title>${this._esc(label)}</text>`;
    }).join("");
    const lines = series
      .map((item) => {
        const points = item.points.filter(
          (point) => point.t >= bounds.start && point.t <= bounds.end,
        );
        const paths = this._seriesSegments(points, item.bucket)
          .map((group) =>
            group.length === 1
              ? `<circle cx="${x(group[0].t)}" cy="${y(group[0].avg)}" r="2.5" fill="${item.color}"/>`
              : `<polyline fill="none" stroke="${item.color}" stroke-width="2" points="${group.map((point) => `${x(point.t).toFixed(2)},${y(point.avg).toFixed(2)}`).join(" ")}"/>`,
          )
          .join("");
        return `<g data-room-signal="${this._esc(item.id)}"><title>${this._esc(item.name)}</title>${paths}</g>`;
      })
      .join("");
    const labels = this._roomsData().groups[gid]?.labels || [];
    const bands = labels
      .filter(
        (label) =>
          label.source === "manual" &&
          label.end > bounds.start &&
          label.start < bounds.end,
      )
      .map(
        (label) =>
          `<rect class="room-label-band ${this._esc(label.state)}" x="${x(Math.max(bounds.start, label.start))}" y="${CHART_MARGIN.top}" width="${x(Math.min(bounds.end, label.end)) - x(Math.max(bounds.start, label.start))}" height="${CHART_PLOT_H}"/>`,
      )
      .join("");
    const empty = series.some((item) => item.points.length)
      ? ""
      : `<text x="${width / 2}" y="${CHART_H / 2}" text-anchor="middle" class="axis-label">No recorded signals in this period</text>`;
    return `<svg viewBox="0 0 ${width} ${CHART_H}" role="img" aria-label="Room signal history"><rect class="plot-bg" x="${CHART_MARGIN.left}" y="${CHART_MARGIN.top}" width="${plotWidth}" height="${CHART_PLOT_H}"/>${bands}${grid}${ticks}${lines}${empty}<rect data-room-graph-selection class="selection-rect" y="${CHART_MARGIN.top}" height="${CHART_PLOT_H}" hidden/><rect data-room-plot-hit class="selection-hit" x="${CHART_MARGIN.left}" y="${CHART_MARGIN.top}" width="${plotWidth}" height="${CHART_PLOT_H}"/></svg>`;
  },

  _paintRoomGraphSelection(section, id, selection) {
    if (!id.startsWith("room:")) return;
    const overlay = section.querySelector("[data-room-graph-selection]");
    if (!overlay) return;
    const bounds = this._historyDayBounds(this._historyStateFor(id).day);
    const visible =
      selection && selection.end > bounds.start && selection.start < bounds.end;
    overlay.hidden = !visible;
    overlay.toggleAttribute("hidden", !visible);
    if (!visible) return;
    const plotWidth =
      overlay.ownerSVGElement.viewBox.baseVal.width -
      CHART_MARGIN.left -
      CHART_MARGIN.right;
    const x = (time) =>
      CHART_MARGIN.left +
      Math.max(
        0,
        Math.min(1, (time - bounds.start) / (bounds.end - bounds.start)),
      ) *
        plotWidth;
    overlay.setAttribute("x", x(selection.start));
    overlay.setAttribute(
      "width",
      Math.max(0, x(selection.end) - x(selection.start)),
    );
  },
};
