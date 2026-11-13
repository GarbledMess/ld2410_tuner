// Zoom a day's graph to a shorter window inside that day. A radar's window is
// its linked chart's (history_graph.js); a room's is kept in its history
// state as `view` (room_graph.js). Both share the label bar (timeline.js).

// The server charts windows of at least six minutes (presentation/charts.py).
const MIN_SPAN = 600;
// A selection is shown with this share of its length either side.
const SELECTION_CONTEXT = 0.1;
const BUTTONS = [
  ["out", "−", "Zoom out"],
  ["in", "+", "Zoom in"],
  ["earlier", "‹ Earlier", "Show earlier"],
  ["later", "Later ›", "Show later"],
  ["selection", "Zoom to selection", ""],
  ["day", "Whole day", ""],
];

export const panelZoom = {
  // The window a room's graph shows: its zoomed view, or the whole day.
  _historyViewBounds(key) {
    const ui = this._historyStateFor(key);
    const day = this._historyDayBounds(ui.day);
    const view = ui.view;
    return view &&
      view.start >= day.start &&
      view.end <= day.end &&
      view.end > view.start
      ? { start: view.start, end: view.end }
      : day;
  },

  _historyZoomHtml() {
    const buttons = BUTTONS.map(
      ([action, text, label]) =>
        `<button type="button" data-history-zoom="${action}"${label ? ` aria-label="${label}"` : ""}>${text}</button>`,
    ).join("");
    // Painted by _paintHistoryZoom, so layout patches keep its state.
    return `<div class="history-zoom" role="group" aria-label="Graph zoom" data-preserve-children>${buttons}<span class="muted" data-history-zoom-label role="status"></span></div>`;
  },

  // Zooming stays inside the day and never moves past the present.
  _zoomLimits(key) {
    const day = this._historyDayBounds(this._historyStateFor(key).day);
    const end = Math.max(
      day.start + MIN_SPAN,
      Math.min(day.end, Date.now() / 1000),
    );
    return { day, start: day.start, end };
  },

  // The window an action moves to, or null for the whole day.
  _zoomTarget(key, action, selection) {
    if (action === "day") return null;
    const limits = this._zoomLimits(key);
    const current = this._historyRangeBounds(key);
    let start = Math.max(limits.start, current.start);
    let end = Math.min(limits.end, current.end);
    if (end <= start) ({ start, end } = limits);
    const span = end - start;
    const middle = (start + end) / 2;
    const around = (center, width) => [center - width / 2, center + width / 2];
    if (action === "in") [start, end] = around(middle, span / 2);
    else if (action === "out") [start, end] = around(middle, span * 2);
    else if (action === "earlier" || action === "later") {
      const shift = (action === "earlier" ? -span : span) / 2;
      [start, end] = [start + shift, end + shift];
    } else if (action === "selection" && selection) {
      const context = (selection.end - selection.start) * SELECTION_CONTEXT;
      [start, end] = [selection.start - context, selection.end + context];
    }
    return this._fitZoom(start, end, limits);
  },

  _fitZoom(start, end, limits) {
    const room = limits.end - limits.start;
    const width = Math.min(room, Math.max(MIN_SPAN, end - start));
    let from = (start + end) / 2 - width / 2;
    from = Math.max(limits.start, Math.min(limits.end - width, from));
    from = Math.round(from);
    const to = Math.min(limits.end, from + Math.round(width));
    if (from <= limits.start && to >= limits.end) return null;
    return { start: from, end: to };
  },

  _wireHistoryZoom(container, key) {
    for (const button of container.querySelectorAll("[data-history-zoom]"))
      button.onclick = () =>
        this._applyHistoryZoom(container, key, button.dataset.historyZoom);
    this._paintHistoryZoom(container, key);
  },

  _applyHistoryZoom(container, key, action) {
    if (this._busyCards.has(key)) return;
    const selection = this._historyRangeValue(container, key);
    const target = this._zoomTarget(key, action, selection);
    const ui = this._historyStateFor(key);
    if (key.startsWith("room:")) {
      ui.view = target;
      this._drawLayout();
      return;
    }
    this._showHistoryWindow(
      container,
      key,
      target || this._historyDayBounds(ui.day),
      selection,
    );
  },

  // Buttons and the window label follow the shown window and selection.
  _paintHistoryZoom(container, key) {
    const zoom = container.querySelector(".history-zoom");
    if (!zoom) return;
    const limits = this._zoomLimits(key);
    const bounds = this._historyRangeBounds(key);
    const future = limits.day.start >= Date.now() / 1000;
    const whole = bounds.start <= limits.start && bounds.end >= limits.end;
    const selection = this._historyRangeValue(container, key);
    const disabled = {
      out: whole,
      day: whole,
      in: bounds.end - bounds.start <= MIN_SPAN,
      earlier: bounds.start <= limits.start,
      later: bounds.end >= limits.end,
      selection:
        !selection ||
        selection.end <= limits.start ||
        selection.start >= limits.end,
    };
    for (const button of zoom.querySelectorAll("[data-history-zoom]")) {
      const off = future || disabled[button.dataset.historyZoom];
      if (button.disabled !== off) button.disabled = off;
    }
    const label = zoom.querySelector("[data-history-zoom-label]");
    const text = whole
      ? "Showing the whole day"
      : `Showing ${this._zoomTime(bounds.start, bounds, limits.day)} – ${this._zoomTime(bounds.end, bounds, limits.day)}`;
    if (label.textContent !== text) label.textContent = text;
  },

  _zoomTime(time, bounds, day) {
    if (time >= day.end) return "24:00";
    return this._axisTime(time, bounds.end - bounds.start);
  },

  // Clock time for a graph spanning `span` seconds; short spans need seconds.
  _axisTime(time, span) {
    return new Date(time * 1000).toLocaleTimeString(undefined, {
      hour: "2-digit",
      minute: "2-digit",
      ...(span <= 1800 ? { second: "2-digit" } : {}),
      hourCycle: "h23",
    });
  },
};
