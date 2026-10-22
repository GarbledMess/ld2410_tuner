import {
  CHART_W,
  CHART_H,
  CHART_MARGIN,
  CHART_PLOT_H,
  GATE_COLORS,
} from "./constants.js";

// Radars in one room chart are told apart by line style; gates keep their colours.
const RADAR_DASHES = ["", "7,4", "2,3", "10,3,2,3"];

export const panelRoomGraph = {
  _roomGraphState(gid) {
    this._roomCharts ||= new Map();
    if (!this._roomCharts.has(gid))
      this._roomCharts.set(gid, {
        kind: "still",
        gate: null,
        request: 0,
        cache: new Map(),
      });
    return this._roomCharts.get(gid);
  },

  _roomGraphGate(gid) {
    const state = this._roomGraphState(gid);
    if (state.gate == null) {
      const first = this._roomsData().groups[gid]?.device_ids.find(
        (id) => this._data?.devices?.[id],
      );
      state.gate = first
        ? this._defaultKindAndGate(this._data.devices[first]).gate
        : 0;
    }
    return state.gate;
  },

  _roomGraphHtml(gid) {
    const state = this._roomGraphState(gid);
    const options = [
      ["move", "Movement"],
      ["still", "Still"],
    ]
      .map(
        ([value, label]) =>
          `<option value="${value}" ${state.kind === value ? "selected" : ""}>${label}</option>`,
      )
      .join("");
    return `<div class="room-graph"><div class="chart-controls"><label>Kind<select data-room-graph-kind>${options}</select></label><button type="button" data-room-graph-refresh>Refresh graph</button></div><div class="gate-chips">${this._roomGateChipsHtml(gid)}</div><div class="room-graph-status muted" data-preserve-children role="status" aria-live="polite"></div><div class="room-signal-canvas" data-room-graph="${this._esc(gid)}" data-chart-canvas="room:${this._esc(gid)}"></div><div class="room-graph-legend" data-preserve-children aria-label="Radars in this graph"></div><p class="muted">Every gate of the selected kind from each radar in this room, each in its own colour; line style identifies the radar. Select a G0–G8 button to highlight that gate with each radar's current threshold. Signals alone do not confirm presence.</p></div>`;
  },

  _roomGateChipsHtml(gid) {
    const gate = this._roomGraphGate(gid);
    return Array.from(
      { length: 9 },
      (_, g) =>
        `<button type="button" class="gate-chip${g === gate ? " active" : ""}" data-room-graph-gate="${g}" aria-label="Highlight gate ${g}" aria-pressed="${g === gate}" style="--chip-color:${GATE_COLORS[g]}">G${g}</button>`,
    ).join("");
  },

  _wireRoomGraph(section, gid, group, foreground = false) {
    const state = this._roomGraphState(gid);
    section.querySelector("[data-room-graph-kind]").onchange = (event) => {
      state.kind = event.target.value;
      this._loadRoomGraph(gid, group, false, true);
    };
    section.querySelector("[data-room-graph-refresh]").onclick = () =>
      this._loadRoomGraph(gid, group, true);
    for (const chip of section.querySelectorAll("[data-room-graph-gate]"))
      chip.onclick = () => {
        state.gate = Number(chip.dataset.roomGraphGate);
        section.querySelector(".gate-chips").innerHTML =
          this._roomGateChipsHtml(gid);
        this._wireRoomGraph(section, gid, group);
        this._renderRoomGraph(gid);
      };
    if (this._roomGraphVisible(gid)) {
      const canvas = section.querySelector("[data-room-graph]");
      if (state.element !== canvas) {
        state.observer?.disconnect();
        state.observer = new ResizeObserver(() => this._renderRoomGraph(gid));
        state.observer.observe(canvas);
        state.element = canvas;
      }
      this._renderRoomGraph(gid);
      this._loadRoomGraph(gid, group, false, foreground);
    }
  },

  _roomGraphSection(gid) {
    return this.shadowRoot.querySelector(
      `[data-room-timeline="${CSS.escape(gid)}"]`,
    );
  },

  _roomGraphVisible(gid) {
    const section = this._roomGraphSection(gid);
    return (
      this.isConnected && !!section && !section.closest("details:not([open])")
    );
  },

  _resumeRoomGraphs(parent) {
    for (const section of parent.querySelectorAll("[data-room-timeline]")) {
      const gid = section.dataset.roomTimeline;
      const group = this._roomsData().groups[gid];
      if (group && this._roomGraphVisible(gid))
        this._wireRoomGraph(section, gid, group, true);
    }
    for (const id of new Set(
      [...parent.querySelectorAll("[data-device-id]")].map(
        (card) => card.dataset.deviceId,
      ),
    )) {
      const device = this._data?.devices?.[id];
      if (device) this._maybeFetchChart(id, device, false);
    }
  },

  async _loadRoomGraph(gid, group, refresh = false, foreground = refresh) {
    if (!this._roomGraphVisible(gid)) return;
    const state = this._roomGraphState(gid);
    const day = this._historyStateFor(`room:${gid}`).day;
    const bounds = this._historyDayBounds(day);
    const currentDay =
      Date.now() / 1000 >= bounds.start && Date.now() / 1000 < bounds.end;
    const ids = group.device_ids.filter((id) => this._data.devices[id]);
    const kind = state.kind;
    const query = JSON.stringify([day, kind, ids]);
    const signature = JSON.stringify([
      day,
      kind,
      ids.map((id) => [id, this._data.devices[id].history?.revision || 0]),
    ]);
    if (
      !refresh &&
      (state.pendingQuery === query ||
        state.failedQuery === query ||
        (!currentDay && state.loadedKey === signature))
    )
      return;
    if (
      !refresh &&
      state.loadedDay === day &&
      state.loadedKind === kind &&
      JSON.stringify(state.data?.series.map((item) => item.id)) ===
        JSON.stringify(ids) &&
      Date.now() - (state.loadedAt || 0) < 15000
    )
      return;
    const retry = refresh && state.failedQuery === query && state.errors;
    const requested = retry
      ? ids.filter((id) => Object.hasOwn(state.errors, id))
      : ids;
    const end = retry
      ? state.retryEnd
      : Math.min(bounds.end, Date.now() / 1000);
    const previous = new Map(
      state.loadedDay === day && state.loadedKind === kind
        ? state.data?.series
            .filter((item) => ids.includes(item.id))
            .map((item) => [item.id, item])
        : [],
    );
    const results = new Map(previous);
    const errors = {};
    const request = ++state.request;
    Object.assign(state, {
      pendingKey: signature,
      pendingQuery: query,
      failedQuery: null,
      loadedKey: null,
      partial: false,
      loading: true,
      error: null,
      errors: {},
      day,
      retryEnd: end,
    });
    if (refresh) state.cache.clear();
    if (state.cache.has(signature) && !refresh && !currentDay) {
      state.data = state.cache.get(signature);
      this._finishRoomGraph(state, signature, day);
      this._renderRoomGraph(gid);
      return;
    }
    this._renderRoomGraph(gid);
    try {
      if (end > bounds.start)
        await Promise.all(
          requested.map(async (id) => {
            try {
              const keys = Array.from(
                { length: 9 },
                (_, gate) => `g${gate}_${kind}`,
              );
              const data = await this._queueChartCall(
                () => {
                  if (!this.isConnected || request !== state.request)
                    return null;
                  return this._chartCall({
                    device_id: id,
                    keys,
                    hours: Math.max(0.1, (end - bounds.start) / 3600),
                    end,
                  });
                },
                { background: !foreground },
              );
              if (!data || request !== state.request) return;
              if (!data.series || typeof data.series !== "object")
                throw new Error(
                  "The tuner returned an invalid signal history. Reload the panel and retry.",
                );
              results.set(id, {
                id,
                name: this._data.devices[id]?.name || "Unavailable radar",
                dash: RADAR_DASHES[ids.indexOf(id) % RADAR_DASHES.length],
                chart: data,
              });
              this._publishRoomSignals(state, bounds, ids, results, day, kind);
              this._renderRoomGraph(gid);
            } catch (error) {
              if (request === state.request)
                errors[id] = error.message || String(error);
            }
          }),
        );
      if (request !== state.request) return;
      state.errors = errors;
      const failed = Object.keys(errors);
      if (failed.length) {
        state.failedQuery = query;
        state.error = failed
          .map(
            (id) =>
              `${this._data.devices[id]?.name || "Unavailable radar"}: ${errors[id]}${previous.has(id) ? " (showing previous signals)" : ""}`,
          )
          .join("; ");
        state.partial = failed.length < ids.length;
      } else {
        this._publishRoomSignals(state, bounds, ids, results, day, kind);
        state.cache.set(signature, state.data);
        while (state.cache.size > 3)
          state.cache.delete(state.cache.keys().next().value);
        this._finishRoomGraph(state, signature, day);
      }
    } finally {
      if (request === state.request) {
        state.loading = false;
        state.pendingKey = null;
        state.pendingQuery = null;
        this._renderRoomGraph(gid);
      }
    }
  },

  _publishRoomSignals(state, bounds, ids, results, day, kind) {
    state.data = {
      bounds,
      series: ids.filter((id) => results.has(id)).map((id) => results.get(id)),
    };
    state.loadedDay = day;
    state.loadedKind = kind;
    state.version = (state.version || 0) + 1;
  },

  _finishRoomGraph(state, signature, day) {
    state.version = (state.version || 0) + 1;
    state.loadedKey = signature;
    state.loadedDay = day;
    state.loadedKind = state.kind;
    state.loadedAt = Date.now();
    state.loading = false;
    state.pendingKey = null;
    state.pendingQuery = null;
  },

  _renderRoomGraph(gid) {
    const section = this._roomGraphSection(gid);
    if (
      !section ||
      !this.isConnected ||
      !this._roomGraphVisible(gid) ||
      this._dragging
    )
      return;
    const state = this._roomGraphState(gid);
    const canvas = section.querySelector("[data-room-graph]");
    const status = section.querySelector(".room-graph-status");
    const paused = (this._roomsData().groups[gid]?.device_ids || [])
      .filter((id) => this._data.devices[id]?.recording_enabled === false)
      .map((id) => this._data.devices[id].name || "Unnamed radar");
    const recording = paused.length
      ? ` Recording paused: ${paused.join(", ")}. Enable Record data in the radar card to resume.`
      : "";
    const statusText = state.loading
      ? "Loading recorded signals. Waiting for the tuner if it is busy…"
      : state.error
        ? `${state.partial ? "Some radar signals unavailable" : "Graph unavailable"}: ${state.error} You can retry with Refresh graph.`
        : `Drag across the graph or the label bar to select a period.${recording}`;
    if (status.textContent !== statusText) status.textContent = statusText;
    status.classList.toggle("is-busy", !!state.loading);
    if (
      !state.data ||
      state.loadedKind !== state.kind ||
      state.loadedDay !== this._historyStateFor(`room:${gid}`).day
    ) {
      canvas.innerHTML =
        state.loading || state.error
          ? ""
          : '<p class="muted">No recorded signals for this day.</p>';
      delete canvas.dataset.paintKey;
      section.querySelector(".room-graph-legend").replaceChildren();
      return;
    }
    const width = Math.max(320, canvas.clientWidth || CHART_W);
    const labels = this._roomsData().groups[gid]?.labels || [];
    const paintKey = JSON.stringify([
      state.version,
      width,
      labels,
      this._roomGraphGate(gid),
      this._roomThresholds(gid, state.data.series),
      !!state.loading,
      !!state.error,
    ]);
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
            `<button type="button" data-room-device="${this._esc(item.id)}"><svg viewBox="0 0 24 10" aria-hidden="true"><line x1="1" y1="5" x2="23" y2="5" style="stroke-dasharray:${item.dash}"/></svg>${this._esc(item.name)}<span>View radar</span></button>`,
        )
        .join("");
    for (const button of legend.querySelectorAll("[data-room-device]"))
      button.onclick = () => {
        const owner = section.closest("[data-node-id], [data-leaf-id]");
        const card = owner.querySelector(
          `[data-device-id="${CSS.escape(button.dataset.roomDevice)}"]`,
        );
        if (card)
          // A lone radar is labelled here, so open its own gate graph instead.
          this._openDeviceSection(
            card,
            button.dataset.roomDevice,
            card.querySelector(".history-range") ? "history" : "chart",
          );
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
    const grid = [0, 25, 50, 75, 100]
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
    const gate = this._roomGraphGate(gid);
    const kind = this._roomGraphState(gid).kind;
    const thresholds = this._roomThresholds(gid, series);
    const lines = series
      .map((item, index) => {
        const shown = { gate, kind };
        const others = this._otherGateLines(item.chart, shown, x, y);
        const { activeBandSvg, activeLineSvg } = this._activeGatePaths(
          item.chart,
          shown,
          `g${gate}_${kind}`,
          x,
          y,
        );
        return `<g data-room-signal="${this._esc(item.id)}" style="stroke-dasharray:${item.dash}"><title>${this._esc(item.name)}</title>${others}${activeBandSvg}${activeLineSvg}${this._roomThresholdSvg(item, index, thresholds[index], gate, width, y)}</g>`;
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
    const empty =
      this._roomGraphState(gid).loading ||
      this._roomGraphState(gid).error ||
      series.some((item) =>
        Object.values(item.chart.series).some((gate) => gate.points.length),
      )
        ? ""
        : `<text x="${width / 2}" y="${CHART_H / 2}" text-anchor="middle" class="axis-label">No recorded signals in this period</text>`;
    return `<svg viewBox="0 0 ${width} ${CHART_H}" role="img" aria-label="Room signal history"><rect class="plot-bg" x="${CHART_MARGIN.left}" y="${CHART_MARGIN.top}" width="${plotWidth}" height="${CHART_PLOT_H}"/>${bands}${grid}${ticks}${lines}${empty}<rect data-room-graph-selection class="selection-rect" y="${CHART_MARGIN.top}" height="${CHART_PLOT_H}" hidden/><rect data-room-plot-hit class="selection-hit" x="${CHART_MARGIN.left}" y="${CHART_MARGIN.top}" width="${plotWidth}" height="${CHART_PLOT_H}"/></svg>`;
  },

  _roomThresholds(gid, series) {
    const key = `g${this._roomGraphGate(gid)}_${this._roomGraphState(gid).kind}`;
    return series.map((item) => {
      const device = this._data.devices[item.id];
      const value = device
        ? this._learningView(item.id, device).current_thresholds?.[key]
        : null;
      return value != null && Number.isFinite(Number(value))
        ? Number(value)
        : null;
    });
  },

  _roomThresholdSvg(item, index, threshold, gate, width, y) {
    if (threshold == null) return "";
    const level = y(threshold);
    return `<line class="threshold-line current" x1="${CHART_MARGIN.left}" x2="${width - CHART_MARGIN.right}" y1="${level}" y2="${level}"/><text class="axis-label current" x="${CHART_MARGIN.left + 4 + index * 130}" y="${Math.max(9, level - 3)}">${this._esc(`${item.name} G${gate} ${this._fmt(threshold)}`)}</text>`;
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
