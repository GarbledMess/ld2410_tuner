const STATUS = {
  present: "Present",
  not_present: "Not present",
  unknown: "Unknown / excluded",
  unlabelled: "No manual label",
};

export const panelHistory = {
  _historyStateFor(id) {
    if (!this._historyState.has(id))
      this._historyState.set(id, {
        day: this._toLocalInputValue(Date.now() / 1000).slice(0, 10),
      });
    return this._historyState.get(id);
  },

  _historyDayBounds(day) {
    const start = new Date(`${day}T00:00:00`);
    const end = new Date(start);
    end.setDate(end.getDate() + 1);
    return { start: start.getTime() / 1000, end: end.getTime() / 1000 };
  },

  _historyPeriods(d, bounds) {
    return (d.history?.labels || [])
      .filter((label) => label.end > bounds.start && label.start < bounds.end)
      .map((label) => ({ ...label }));
  },

  _historyDaySegments(periods, bounds) {
    const times = [
      ...new Set([
        bounds.start,
        bounds.end,
        ...periods.flatMap((label) => [
          Math.max(bounds.start, label.start),
          Math.min(bounds.end, label.end),
        ]),
      ]),
    ].sort((a, b) => a - b);
    return times.slice(0, -1).map((start, index) => ({
      start,
      end: times[index + 1],
      state:
        periods.findLast((label) => label.start <= start && label.end > start)
          ?.state || "unlabelled",
    }));
  },

  _historyTime(timestamp) {
    return new Date(timestamp * 1000).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      timeZoneName: "short",
    });
  },

  _historyInputValue(timestamp) {
    const date = new Date(timestamp * 1000);
    const seconds = date.getSeconds();
    return (
      this._toLocalInputValue(timestamp) +
      (seconds ? `:${String(seconds).padStart(2, "0")}` : "")
    );
  },

  _historyHtml(id, d) {
    const ui = this._historyStateFor(id);
    const bounds = this._historyDayBounds(ui.day);
    const periods = this._historyPeriods(d, bounds);
    const segments = this._historyDaySegments(periods, bounds);
    const timeline = segments
      .map((segment) => {
        const width =
          ((segment.end - segment.start) / (bounds.end - bounds.start)) * 100;
        const title = `${this._historyTime(segment.start)} – ${this._historyTime(segment.end)}: ${STATUS[segment.state]}`;
        return `<span class="history-segment ${segment.state}" style="width:${width}%" title="${this._esc(title)}"></span>`;
      })
      .join("");
    const rows = periods
      .sort((a, b) => a.start - b.start)
      .map((label) => this._historyPeriodHtml(label));
    const active = ["present", "not_present"].includes(d.training_state)
      ? `<p class="muted">Live training is ${this._esc(STATUS[d.training_state])}. Its current period is still open; saved periods appear below once closed.</p>`
      : "";
    return `<div class="muted">Review manual labels in the stored ${d.history?.retention_days || 30}-day history. Times use ${this._esc(Intl.DateTimeFormat().resolvedOptions().timeZone)}.</div>
      ${this._historyCalendarHtml(id, d)}
      <div class="history-navigation">
        <button type="button" data-action="history-previous" aria-label="Previous day">‹</button>
        <label>Day<input type="date" data-action="history-day" value="${ui.day}"></label>
        <button type="button" data-action="history-next" aria-label="Next day">›</button>
        <button type="button" data-action="history-today">Today</button>
      </div>
      <div class="history-timeline" role="img" aria-label="Manual label coverage for ${ui.day}">${timeline}</div>
      <div class="history-axis"><span>${this._esc(this._historyTime(bounds.start))}</span><span>${this._esc(this._historyTime(bounds.end))} (next day)</span></div>
      <div class="history-legend">${Object.entries(STATUS)
        .map(
          ([state, name]) =>
            `<span><i class="history-segment ${state}"></i>${name}</span>`,
        )
        .join("")}</div>
      ${active}
      <div class="history-periods">${rows.join("") || '<p class="muted">No saved manual periods on this day. Unlabelled time may still contain automatic estimates.</p>'}</div>
      <p class="history-edit-note" role="status">${ui.edit ? "Editing the full saved period, including any portion on another day." : "Add a period, or select a saved period to edit its times and status."}</p>
      <div class="history-fields">
        <label>From<input data-action="history-start" type="datetime-local" step="1"></label>
        <label>To<input data-action="history-end" type="datetime-local" step="1"></label>
        <label>Status<select data-action="history-state"><option value="present">Present</option><option value="not_present">Not present</option><option value="unknown">Unknown / exclude</option></select></label>
      </div>
      <div class="history-buttons">
        <button type="button" data-action="history-apply" class="primary">${ui.edit ? "Save changes" : "Label time range"}</button>
        <button type="button" data-action="history-remove" ${ui.edit ? "" : "hidden"}>Remove label</button>
        <button type="button" data-action="history-new">New period</button>
      </div>
      <p class="muted">Saving replaces labels in the chosen range. Unknown excludes that range from learning. Removing a label lets any stored automatic estimates be used again.</p>`;
  },

  _historyPeriodHtml(label) {
    const from = new Date(label.start * 1000).toLocaleString();
    const to = new Date(label.end * 1000).toLocaleString();
    return `<button type="button" class="history-period" data-action="history-edit" data-start="${Number(label.start)}" data-end="${Number(label.end)}">
      <span>${this._esc(from)} → ${this._esc(to)}</span>
      <b class="history-status ${this._esc(label.state)}">${this._esc(STATUS[label.state])}</b><span>Edit</span>
    </button>`;
  },

  _wireHistory(card, id) {
    this._wireHistoryCalendar(card, id);
    const day = card.querySelector('[data-action="history-day"]');
    day.onchange = () => {
      if (day.value) this._changeHistoryDay(card, id, day.value);
    };
    for (const [action, offset] of [
      ["previous", -1],
      ["next", 1],
    ])
      card.querySelector(`[data-action="history-${action}"]`).onclick = () => {
        const date = new Date(`${this._historyStateFor(id).day}T12:00:00`);
        date.setDate(date.getDate() + offset);
        this._changeHistoryDay(
          card,
          id,
          this._toLocalInputValue(date.getTime() / 1000).slice(0, 10),
        );
      };
    card.querySelector('[data-action="history-today"]').onclick = () =>
      this._changeHistoryDay(
        card,
        id,
        this._toLocalInputValue(Date.now() / 1000).slice(0, 10),
      );
    card.querySelectorAll('[data-action="history-edit"]').forEach((button) => {
      button.onclick = () =>
        this._selectHistoryPeriod(
          card,
          id,
          Number(button.dataset.start),
          Number(button.dataset.end),
        );
    });
    card.querySelector('[data-action="history-new"]').onclick = () =>
      this._newHistoryPeriod(card, id);
    card.querySelector('[data-action="history-apply"]').onclick = (event) =>
      this._action(event.currentTarget, () =>
        this._saveHistoryPeriod(card, id),
      );
    card.querySelector('[data-action="history-remove"]').onclick = (event) =>
      this._action(event.currentTarget, () => this._removeHistoryPeriod(id));
  },

  _refreshHistorySection(card, id) {
    this._captureDrafts(this.shadowRoot.querySelector("#grid"));
    card.querySelector('[data-section="history"] .subsection-body').innerHTML =
      this._historyHtml(id, this._data.devices[id]);
    this._wireHistory(card, id);
    this._restoreDraft(card, id);
  },

  _changeHistoryDay(card, id, day) {
    const ui = this._historyStateFor(id);
    ui.day = day;
    ui.month = day.slice(0, 7);
    this._refreshHistorySection(card, id);
  },

  _selectHistoryPeriod(card, id, start, end) {
    const device = this._data.devices[id];
    const label = device.history.labels.find(
      (item) => item.start === start && item.end === end,
    );
    if (!label) return;
    this._historyStateFor(id).edit = {
      ...label,
      revision: device.history.revision || 0,
    };
    this._refreshHistorySection(card, id);
    for (const [name, value] of [
      ["start", this._historyInputValue(start)],
      ["end", this._historyInputValue(end)],
      ["state", label.state],
    ])
      card.querySelector(`[data-action="history-${name}"]`).value = value;
  },

  _newHistoryPeriod(card, id) {
    this._historyStateFor(id).edit = null;
    this._refreshHistorySection(card, id);
    for (const name of ["start", "end"])
      card.querySelector(`[data-action="history-${name}"]`).value = "";
  },

  _historyFieldTime(card, name, original) {
    const value = card.querySelector(`[data-action="history-${name}"]`).value;
    // Preserve seconds and the correct occurrence of an ambiguous DST hour
    // when an existing boundary has not been changed in the form.
    if (original != null && value === this._historyInputValue(original))
      return original;
    return new Date(value).getTime() / 1000;
  },

  async _saveHistoryPeriod(card, id) {
    const edit = this._historyStateFor(id).edit;
    const start = this._historyFieldTime(card, "start", edit?.start);
    const end = this._historyFieldTime(card, "end", edit?.end);
    if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start)
      throw new Error(
        "Choose a start and end time, with the end after the start.",
      );
    const state = card.querySelector('[data-action="history-state"]').value;
    if (
      !confirm(
        `${edit ? "Update this saved period" : "Label this range"} as ${STATUS[state]}? Overlapping labels in the chosen range will be replaced.`,
      )
    )
      return;
    const payload = { device_id: id, start, end, state };
    if (edit)
      Object.assign(payload, {
        label_start: edit.start,
        label_end: edit.end,
        revision: edit.revision,
      });
    await this._call(edit ? "edit_history_label" : "label_history", payload);
    await this._historyChanged(id);
  },

  async _removeHistoryPeriod(id) {
    const edit = this._historyStateFor(id).edit;
    if (
      !edit ||
      !confirm(
        "Remove this manual label? Stored automatic estimates in this period can be used again. Recorded energy data is kept.",
      )
    )
      return;
    await this._call("edit_history_label", {
      device_id: id,
      label_start: edit.start,
      label_end: edit.end,
      start: edit.start,
      end: edit.end,
      state: "unlabelled",
      revision: edit.revision,
    });
    await this._historyChanged(id);
  },

  async _historyChanged(id) {
    this._historyStateFor(id).edit = null;
    const chart = this._chartState.get(id);
    chart.data = null;
    chart.cache?.clear();
    chart.error = null;
    await this._load();
  },
};
