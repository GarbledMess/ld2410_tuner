export const panelHistoryGraph = {
  _placeHistoryGraph(card, id) {
    const chart = card.querySelector('.subsection[data-section="chart"]');
    const linked = !this._sectionCollapsed(id, "history");
    const target = card.querySelector(
      linked ? ".history-graph" : ".chart-home",
    );
    if (chart && target && chart.parentElement !== target) target.append(chart);
    card.querySelector(".chart-home").hidden = linked;
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

  _reviewHistoryRange(id, start, end) {
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
