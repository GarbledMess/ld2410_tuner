export const panelCalendar = {
  _historyCalendarHtml(id, d) {
    const ui = this._historyStateFor(id);
    const month = ui.month || ui.day.slice(0, 7);
    const first = new Date(`${month}-01T12:00:00`);
    const title = first.toLocaleDateString([], {
      month: "long",
      year: "numeric",
    });
    const start = new Date(first);
    start.setDate(1 - ((first.getDay() + 6) % 7));
    const today = this._toLocalInputValue(Date.now() / 1000).slice(0, 10);
    const weekdays = Array.from({ length: 7 }, (_, index) => {
      const date = new Date(2024, 0, 1 + index);
      return `<span class="calendar-weekday">${this._esc(date.toLocaleDateString([], { weekday: "short" }))}</span>`;
    }).join("");
    const days = Array.from({ length: 42 }, (_, index) => {
      const date = new Date(start);
      date.setDate(start.getDate() + index);
      return this._historyCalendarDay(date, month, ui.day, today, d);
    }).join("");
    return `<div class="history-calendar" role="group" aria-label="Presence label calendar">
      <div class="calendar-heading">
        <button type="button" data-action="calendar-previous" aria-label="Previous month">‹</button>
        <b data-role="calendar-month">${this._esc(title)}</b>
        <button type="button" data-action="calendar-next" aria-label="Next month">›</button>
      </div>
      <div class="calendar-grid">${weekdays}${days}</div>
      <div class="muted">Dots show saved manual statuses. Select a date to review its timeline.</div>
    </div>`;
  },

  _historyCalendarDay(date, month, selected, today, d) {
    const day = this._toLocalInputValue(date.getTime() / 1000).slice(0, 10);
    const bounds = this._historyDayBounds(day);
    const states = new Set(
      this._historyPeriods(d, bounds).map((label) => label.state),
    );
    const marks = ["present", "not_present", "unknown"].filter((state) =>
      states.has(state),
    );
    const dots = marks
      .map((state) => `<i class="history-segment ${state}"></i>`)
      .join("");
    const statuses =
      marks
        .map((state) => state.replace("not_present", "not present"))
        .join(", ") || "no saved labels";
    const label = `${date.toLocaleDateString([], { weekday: "long", day: "numeric", month: "long", year: "numeric" })}: ${statuses}`;
    const classes = ["calendar-day"];
    if (!day.startsWith(month)) classes.push("other-month");
    if (day === today) classes.push("today");
    return `<button type="button" class="${classes.join(" ")}" data-action="calendar-day" data-day="${day}" aria-pressed="${day === selected}" aria-label="${this._esc(label)}">
      <span>${date.getDate()}</span><span class="calendar-dots" aria-hidden="true">${dots}</span>
    </button>`;
  },

  _wireHistoryCalendar(card, id) {
    card.querySelectorAll('[data-action="calendar-day"]').forEach((button) => {
      button.onclick = () =>
        this._changeHistoryDay(card, id, button.dataset.day);
    });
    for (const [direction, offset] of [
      ["previous", -1],
      ["next", 1],
    ])
      card.querySelector(`[data-action="calendar-${direction}"]`).onclick =
        () => {
          const ui = this._historyStateFor(id);
          const month = new Date(
            `${ui.month || ui.day.slice(0, 7)}-01T12:00:00`,
          );
          month.setMonth(month.getMonth() + offset);
          ui.month = this._toLocalInputValue(month.getTime() / 1000).slice(
            0,
            7,
          );
          this._refreshHistorySection(card, id);
        };
  },
};
