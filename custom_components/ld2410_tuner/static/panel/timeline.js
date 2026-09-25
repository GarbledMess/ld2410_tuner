// The timeline uses the graph's pointer capture and cancellation lifecycle.
export const panelTimeline = {
  _historyRangeHtml(timeline, day) {
    return `<div class="history-range" role="group" aria-label="Select a period on ${day}">
      <div class="history-timeline" role="img" aria-label="Manual label coverage for ${day}">${timeline}</div>
      <span class="history-range-selection" hidden></span>
      ${["start", "end"].map((boundary) => `<button type="button" class="history-range-handle" data-boundary="${boundary}" role="slider" aria-label="Selection ${boundary}" hidden></button>`).join("")}
      </div><div class="muted">Drag across the bar to select a period, then drag either end or edit the times below.</div>`;
  },

  _historyRangeValue(card, id) {
    const ui = this._historyStateFor(id);
    const edit = ui.range || ui.edit;
    const start = this._historyFieldTime(card, "start", edit?.start);
    const end = this._historyFieldTime(card, "end", edit?.end);
    return Number.isFinite(start) && Number.isFinite(end) && end > start
      ? { start, end }
      : null;
  },

  _paintHistoryRange(card, id, selection = this._historyRangeValue(card, id)) {
    const bar = card.querySelector(".history-range");
    const bounds = this._historyDayBounds(this._historyStateFor(id).day);
    const visible =
      selection && selection.end > bounds.start && selection.start < bounds.end;
    const overlay = bar.querySelector(".history-range-selection");
    overlay.hidden = !visible;
    const percent = (time) =>
      (100 *
        (Math.max(bounds.start, Math.min(bounds.end, time)) - bounds.start)) /
      (bounds.end - bounds.start);
    if (visible) {
      overlay.style.left = `${percent(selection.start)}%`;
      overlay.style.width = `${percent(selection.end) - percent(selection.start)}%`;
    }
    for (const handle of bar.querySelectorAll(".history-range-handle")) {
      const time = selection?.[handle.dataset.boundary];
      handle.hidden = !visible || time < bounds.start || time > bounds.end;
      if (handle.hidden) continue;
      handle.style.setProperty("--position", `${percent(time)}%`);
      handle.setAttribute("aria-valuemin", bounds.start);
      handle.setAttribute("aria-valuemax", bounds.end);
      handle.setAttribute("aria-valuenow", time);
      handle.setAttribute("aria-valuetext", this._historyTime(time));
    }
  },

  _setHistoryRange(card, id, selection, newPeriod = false) {
    if (newPeriod) this._newHistoryPeriod(card, id);
    this._historyStateFor(id).range = { ...selection };
    for (const boundary of ["start", "end"])
      card.querySelector(`[data-action="history-${boundary}"]`).value =
        this._historyInputValue(selection[boundary]);
    this._paintHistoryRange(card, id, selection);
    this._captureDrafts(this.shadowRoot.querySelector("#grid"));
  },

  _wireHistoryRange(card, id) {
    const bar = card.querySelector(".history-range");
    bar.onpointerdown = (event) => this._dragHistoryRange(event, card, id);
    for (const handle of bar.querySelectorAll(".history-range-handle"))
      handle.onkeydown = (event) => this._keyHistoryRange(event, card, id);
    for (const boundary of ["start", "end"])
      card.querySelector(`[data-action="history-${boundary}"]`).oninput = () =>
        this._paintHistoryRange(card, id);
    this._paintHistoryRange(card, id);
  },

  _dragHistoryRange(event, card, id) {
    if (!this._selectionPointerAllowed(event)) return;
    const bar = event.currentTarget;
    const boundary = event.target.closest("[data-boundary]")?.dataset.boundary;
    const previous = this._historyRangeValue(card, id);
    if (boundary && !previous) return;
    event.preventDefault();
    const bounds = this._historyDayBounds(this._historyStateFor(id).day);
    const rect = bar.getBoundingClientRect();
    const timeAt = (x) =>
      Math.max(
        bounds.start,
        Math.min(
          bounds.end,
          Math.round(
            (bounds.start +
              ((x - rect.left) / rect.width) * (bounds.end - bounds.start)) /
              60,
          ) * 60,
        ),
      );
    const anchor = timeAt(event.clientX);
    const selectionAt = (x) => {
      const time = timeAt(x);
      if (boundary === "start")
        return { ...previous, start: Math.min(time, previous.end - 1) };
      if (boundary === "end")
        return { ...previous, end: Math.max(time, previous.start + 1) };
      return { start: Math.min(anchor, time), end: Math.max(anchor, time) };
    };
    const restore = () => this._paintHistoryRange(card, id, previous);
    this._beginDrag(
      event,
      (next) => {
        next.preventDefault();
        this._paintHistoryRange(card, id, selectionAt(next.clientX));
      },
      (next) => {
        const selection = selectionAt(next.clientX);
        if (
          (!boundary && Math.abs(next.clientX - event.clientX) < 4) ||
          selection.end <= selection.start
        ) {
          restore();
          return;
        }
        this._setHistoryRange(card, id, selection, !boundary);
      },
      restore,
    );
  },

  _keyHistoryRange(event, card, id) {
    if (this._busyCards.has(id)) return;
    const offset = { ArrowLeft: -1, ArrowRight: 1 }[event.key];
    if (!offset) return;
    const selection = this._historyRangeValue(card, id);
    if (!selection) return;
    event.preventDefault();
    const boundary = event.currentTarget.dataset.boundary;
    const bounds = this._historyDayBounds(this._historyStateFor(id).day);
    const minimum = boundary === "start" ? bounds.start : selection.start + 1;
    const maximum = boundary === "end" ? bounds.end : selection.end - 1;
    selection[boundary] = Math.max(
      minimum,
      Math.min(
        maximum,
        selection[boundary] + offset * (event.shiftKey ? 900 : 60),
      ),
    );
    this._setHistoryRange(card, id, selection);
  },
};
