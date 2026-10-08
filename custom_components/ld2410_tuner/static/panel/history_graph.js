export const panelHistoryGraph = {
  _placeHistoryGraph(card, id) {
    const chart = card.querySelector('.subsection[data-section="chart"]');
    const linked = !this._sectionCollapsed(id, "history");
    const target = card.querySelector(
      linked ? ".history-graph" : ".chart-home",
    );
    if (chart && target && chart.parentElement !== target) target.append(chart);
    this._placeHistoryTimeline(card, chart, linked);
    card.querySelector(".chart-home").hidden = linked;
    requestAnimationFrame(() => this._syncHistoryTimeline(card, id));
  },

  _placeHistoryTimeline(card, chart, linked) {
    const editor = card.querySelector(
      '.subsection[data-section="history"] > .subsection-body',
    );
    const fresh = editor.querySelector(":scope > .history-timeline-editor");
    const existing = chart.querySelector(".history-timeline-editor");
    if (fresh && existing) existing.remove();
    const timeline = fresh || existing;
    if (!timeline) return;
    if (linked) chart.querySelector(".chart-legend").before(timeline);
    else editor.querySelector(".history-graph").after(timeline);
  },

  _syncHistoryTimeline(card, id) {
    if (!card.isConnected || this._sectionCollapsed(id, "history")) return;
    const bounds = this._historyRangeBounds(id);
    const periods = this._historyPeriods(this._data.devices[id], bounds);
    const bar = card.querySelector(".history-range");
    this._updateHtml(
      bar.querySelector(".history-timeline"),
      this._historySegmentsHtml(periods, bounds),
    );
    const axis = card.querySelector(".history-axis");
    this._updateHtml(
      axis,
      [bounds.start, bounds.end]
        .map(
          (time) =>
            `<span>${this._esc(new Date(time * 1000).toLocaleString())}</span>`,
        )
        .join(""),
    );
    this._alignHistoryTimeline(card, bar, axis);
    this._paintHistoryRange(card, id);
  },

  _alignHistoryTimeline(card, bar, axis) {
    const plot = card.querySelector('[data-role="selection-hit"]');
    if (!plot) return;
    const rect = plot.getBoundingClientRect();
    const parent = bar.parentElement.getBoundingClientRect();
    const style = getComputedStyle(bar.parentElement);
    const left = parent.left + Number.parseFloat(style.paddingLeft);
    const right = parent.right - Number.parseFloat(style.paddingRight);
    if (!rect.width) return;
    for (const element of [bar, axis]) {
      element.style.marginLeft = `${rect.left - left}px`;
      element.style.marginRight = `${right - rect.right}px`;
    }
  },

  _observeHistoryPlot(id, canvas) {
    const cs = this._chartState.get(id);
    cs.plotObserver?.disconnect();
    // Resolve the card each time: polling moves this canvas into a new card.
    cs.plotObserver = new ResizeObserver(() => {
      const card = canvas.closest("[data-device-id]");
      if (card) this._syncHistoryTimeline(card, id);
    });
    cs.plotObserver.observe(canvas);
    this._syncHistoryTimeline(canvas.closest("[data-device-id]"), id);
  },

  _showHistoryWindow(card, id, bounds, selection = null) {
    const cs = this._chartState.get(id);
    if (!cs) return;
    const end = Math.min(bounds.end, Date.now() / 1000);
    cs.historyLinked = true;
    cs.end = end;
    cs.hours = Math.max(0.1, Math.min(720, (end - bounds.start) / 3600));
    cs.selection = selection;
    cs.panelOpen = false;
    this._setSectionCollapsed(id, "chart", false);
    const chart = card.querySelector('.subsection[data-section="chart"]');
    chart.classList.remove("collapsed");
    chart.querySelector(".sub-toggle").textContent = "▾";
    chart.querySelector(".sub-toggle").setAttribute("aria-expanded", "true");
    this._placeHistoryGraph(card, id);
    if (end <= bounds.start) {
      // Invalidate older in-flight responses when navigating to a future day.
      cs.request = (cs.request || 0) + 1;
      cs.loading = false;
      cs.data = null;
      cs.error = "This day has no recorded history yet.";
      this._renderChartCanvas(id, true);
      return;
    }
    this._fetchChartData(id);
  },

  _reviewHistoryRange(id, start, end, state = "present", gateKey = "") {
    const card = this.shadowRoot.querySelector(
      `[data-device-id="${CSS.escape(id)}"]`,
    );
    if (
      !card ||
      !Number.isFinite(start) ||
      !Number.isFinite(end) ||
      end <= start
    )
      return;
    this._setSectionCollapsed(id, "history", false);
    const section = card.querySelector('.subsection[data-section="history"]');
    section.classList.remove("collapsed");
    section.querySelector(".sub-toggle").textContent = "▾";
    section.querySelector(".sub-toggle").setAttribute("aria-expanded", "true");
    this._historyStateFor(id).day = this._toLocalInputValue(start).slice(0, 10);
    this._historyStateFor(id).month = this._historyStateFor(id).day.slice(0, 7);
    this._setHistoryRange(card, id, { start, end }, true);
    card.querySelector('[data-action="history-state"]').value = state;
    this._captureDrafts(this.shadowRoot.querySelector("#grid"));
    const focus = /^g([0-8])_(move|still)$/.exec(gateKey);
    if (focus)
      Object.assign(this._chartState.get(id), {
        gate: Number(focus[1]),
        kind: focus[2],
      });
    this._focusHistoryPeriod(card, id, { start, end });
    card
      .querySelector(".history-graph")
      .scrollIntoView({ behavior: "smooth", block: "start" });
  },

  _focusHistoryPeriod(card, id, range) {
    const padding = Math.max(300, (range.end - range.start) * 0.1);
    this._showHistoryWindow(
      card,
      id,
      { start: range.start - padding, end: range.end + padding },
      range,
    );
  },

  _syncHistorySelection(card, id) {
    const cs = this._chartState.get(id);
    if (!cs?.historyLinked) return;
    cs.selection = this._historyRangeValue(card, id);
    cs.panelOpen = false;
    this._renderChartCanvas(id, true);
  },

  _useGraphSelection(id, selection) {
    const card = this.shadowRoot.querySelector(
      `[data-device-id="${CSS.escape(id)}"]`,
    );
    if (!card) return;
    this._setHistoryRange(card, id, selection);
  },
};
