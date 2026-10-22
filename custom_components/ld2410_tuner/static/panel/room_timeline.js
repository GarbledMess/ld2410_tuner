// A group's room labels (spec 4.1): a day timeline that reuses the radar
// cards' range editor (timeline.js) and drag lifecycle (selection.js), the
// "Seen by" chips of each occupied episode (4.3), the room estimate with
// "Was this right?" (4.4) and the room presence sources (4.5). Timeline state
// lives in the shared history state under "room:<group id>".
import { ROOM_STATUS } from "./history.js";

// "high" attribution confidence; below it a chip reads "low".
const HIGH_CONFIDENCE = 0.8;
// A tapped chip shows "updating" until the rebuilt attribution lands, or
// this long if the rebuild changes nothing.
const PENDING_MS = 60000;

function seenLevel(result) {
  if (result.state === "unknown") return "unknown";
  return result.confidence >= HIGH_CONFIDENCE ? "high" : "low";
}

export const panelRoomTimeline = {
  _roomTimelineHtml(gid, group) {
    const rid = `room:${gid}`;
    const ui = this._historyStateFor(rid);
    const bounds = this._historyDayBounds(ui.day);
    const inDay = (label) =>
      label.end > bounds.start && label.start < bounds.end;
    const labels = (group.labels || []).filter(inDay);
    const manual = labels.filter((label) => label.source === "manual");
    const sensor = labels.filter((label) => label.source !== "manual");
    const rows = [...manual]
      .sort((a, b) => a.start - b.start)
      .map((label) => this._roomPeriodHtml(label))
      .join("");
    const fields = this._roomFieldValues(ui);
    const options = Object.entries(ROOM_STATUS)
      .map(
        ([state, name]) =>
          `<option value="${state}" ${fields.state === state ? "selected" : ""}>${name}</option>`,
      )
      .join("");
    return `<section class="room-labels" data-room-timeline="${this._esc(gid)}"><h4>Room presence</h4>
      <p class="muted">Review the signals, then label when this ${group.kind === "zone" ? "zone" : "room"} was present or empty.</p>
      <div class="history-navigation">
        <button type="button" data-room-label-action="previous" aria-label="Previous day">‹</button>
        <label>Day<input type="date" data-room-label-action="day" value="${ui.day}"></label>
        <button type="button" data-room-label-action="next" aria-label="Next day">›</button>
        <button type="button" data-room-label-action="today">Today</button>
      </div>
      ${this._roomGraphHtml(gid)}
      <div class="history-timeline-editor">${this._historyRangeHtml(this._historySegmentsHtml(manual, bounds), ui.day)}
      <div class="history-axis"><span>${this._esc(this._historyTime(bounds.start))}</span><span>${this._esc(this._historyTime(bounds.end))} (next day)</span></div></div>
      ${this._roomSensorBarHtml(sensor, bounds)}
      <div class="history-periods">${rows || '<p class="muted">No manual room labels on this day.</p>'}</div>
      ${this._loneRadarLabelsHtml(group)}
      <p class="history-edit-note" role="status">${ui.edit ? "Editing the full saved room period." : "Add a period, or select a saved period to edit it."}</p>
      <div class="history-fields">
        <label>From<input data-action="history-start" type="datetime-local" step="1" value="${this._esc(fields.start)}"></label>
        <label>To<input data-action="history-end" type="datetime-local" step="1" value="${this._esc(fields.end)}"></label>
        <label>Status<select data-room-label-action="state">${options}</select></label>
      </div>
      <div class="history-buttons">
        <button type="button" data-room-label-action="apply" class="primary">${ui.edit ? "Save changes" : "Label time range"}</button>
        <button type="button" data-room-label-action="remove" ${ui.edit ? "" : "hidden"}>Remove label</button>
        <button type="button" data-room-label-action="new">New period</button>
      </div>
      ${this._seenByHtml(gid, bounds)}${this._roomEstimateHtml(gid, group)}${this._roomConflictsHtml(gid, bounds)}</section>`;
  },

  // Field values survive polls: captured from the page before each redraw,
  // otherwise taken from the selected range or saved period.
  _roomFieldValues(ui) {
    if (ui.fields) return ui.fields;
    const bounds = this._historyDayBounds(ui.day);
    const now = Date.now() / 1000;
    const period = ui.range ||
      ui.edit || {
        start: bounds.start,
        end: now > bounds.start ? Math.min(bounds.end, now) : bounds.end,
      };
    const value = (time) => this._historyInputValue(time);
    return {
      start: value(period?.start),
      end: value(period?.end),
      state: ui.edit?.state || "present",
    };
  },

  _captureRoomFields(body) {
    const section = body.querySelector("[data-room-timeline]");
    if (!section) return;
    const field = (selector) => section.querySelector(selector).value;
    this._historyStateFor(`room:${section.dataset.roomTimeline}`).fields = {
      start: field('[data-action="history-start"]'),
      end: field('[data-action="history-end"]'),
      state: field('[data-room-label-action="state"]'),
    };
  },

  _roomSensorBarHtml(sensor, bounds) {
    if (!sensor.length) return "";
    const segments = this._historyDaySegments(sensor, bounds)
      .map((segment) => {
        const width =
          ((segment.end - segment.start) / (bounds.end - bounds.start)) * 100;
        const name = ROOM_STATUS[segment.state] || "No sensor label";
        const title = `${this._historyTime(segment.start)} – ${this._historyTime(segment.end)}: ${name}`;
        return `<span class="history-segment ${segment.state} sensor" style="width:${width}%" title="${this._esc(title)}"></span>`;
      })
      .join("");
    return `<div class="room-sensor-labels"><span class="muted">Sensor labels (read-only, from room presence sources)</span><div class="history-timeline" role="img" aria-label="Sensor room labels">${segments}</div></div>`;
  },

  _roomPeriodHtml(label) {
    const from = new Date(label.start * 1000).toLocaleString();
    const to = new Date(label.end * 1000).toLocaleString();
    return `<button type="button" class="history-period" data-room-label-action="edit" data-start="${Number(label.start)}" data-end="${Number(label.end)}">
      <span>${this._esc(from)} → ${this._esc(to)}</span>
      <b class="history-status ${this._esc(label.state)}">${this._esc(ROOM_STATUS[label.state])}</b><span>Edit</span>
    </button>`;
  },

  _seenByHtml(gid, bounds) {
    const all = this._data?.room_attribution?.[gid] || [];
    const pending = this._pendingSeenBy(gid, all);
    const episodes = all.filter(
      (episode) => episode.end > bounds.start && episode.start < bounds.end,
    );
    if (!episodes.length) return "";
    const rows = episodes.map((episode) => {
      const chips = Object.entries(episode.devices)
        .map(([id, result]) =>
          this._seenByChipHtml(
            id,
            result,
            episode,
            pending.has(`${gid}|${id}|${episode.start}`),
          ),
        )
        .join(" ");
      const estimated = episode.estimated
        ? ' <span class="muted">(estimated)</span>'
        : "";
      return `<div class="seen-by"><span class="muted">${this._esc(this._historyTime(episode.start))} – ${this._esc(this._historyTime(episode.end))}</span>${estimated} <span>Seen by:</span> ${chips}</div>`;
    });
    return `<div class="seen-by-list"><b>Occupied periods</b><p class="muted">Tap a radar to correct whether it saw that period; this saves a label on that radar.</p>${rows.join("")}</div>`;
  },

  // Chips tapped since the attribution last changed (or within PENDING_MS).
  _pendingSeenBy(gid, episodes) {
    this._seenByTaps ||= new Map();
    const current = JSON.stringify(episodes);
    for (const [key, tap] of this._seenByTaps)
      if (
        key.startsWith(`${gid}|`) &&
        (tap.episodes !== current || Date.now() - tap.at > PENDING_MS)
      )
        this._seenByTaps.delete(key);
    return this._seenByTaps;
  },

  _seenByChipHtml(id, result, episode, pending) {
    const level = seenLevel(result);
    const seen = result.state === "yes";
    const name = this._roomsData().members[id]?.name || id;
    // Rule 2: decided from the radar's baseline (a weighted guess), not its own label.
    const basis =
      result.inferred && result.state !== "unknown" ? ", inferred" : "";
    const text =
      result.state === "no"
        ? `${name}: not seen (${level}${basis})`
        : `${name} (${level}${basis})`;
    const busy = pending ? ' aria-busy="true" disabled' : "";
    return `<button type="button" class="seen-by-chip${seen ? " seen" : ""}" data-seen-by="${this._esc(id)}" data-start="${Number(episode.start)}" data-end="${Number(episode.end)}" aria-pressed="${seen}"${busy}>${this._esc(text)}${pending ? " · updating…" : ""}</button>`;
  },

  // The server finds the estimate and its stretch (rooms/room_labels.py).
  _roomEstimateHtml(gid, group) {
    const estimate = group.estimate;
    if (!estimate) return "";
    return `<div class="room-estimate notice">Room estimate: ${ROOM_STATUS[estimate.state]} (${Math.round(estimate.confidence * 100)}%) since ${this._esc(this._historyTime(estimate.start))}. Was this right?
      <button type="button" class="fb-btn correct" data-room-estimate="true">Yes</button>
      <button type="button" class="fb-btn incorrect" data-room-estimate="false">No</button></div>`;
  },

  _roomSourcesHtml(gid, group) {
    return this._sourcesHtml(
      `room:${gid}`,
      { presence_sources: group.presence_sources },
      gid,
    );
  },

  // A lone radar's own labels win over this timeline (rooms/lone_labels.py): say so,
  // and let them be edited, since its card shows no editor of its own.
  _loneRadarLabelsHtml(group) {
    const recording = this._recordingMembers(group);
    const [id] = recording;
    const device = this._data?.devices?.[id];
    if (recording.length !== 1 || !device || this._radarEditorShown.has(id))
      return "";
    const name = this._esc(device.name || "this radar");
    const count = device.history?.labels?.length || 0;
    const live = { present: "Present", not_present: "Not present" }[
      device.training_state
    ];
    const parts = [
      live && device.training_label_start != null
        ? `${name} has a live ${live} label of its own, which takes priority.`
        : "",
      count
        ? `${count === 1 ? "1 period is" : `${count} periods are`} labelled on ${name} itself and ${count === 1 ? "takes" : "take"} priority.`
        : "",
    ].filter(Boolean);
    if (!parts.length) return "";
    return `<p class="field-note" data-radar-labels>${parts.join(" ")} <button type="button" data-room-action="edit-radar-labels" data-device="${this._esc(id)}">Edit radar labels</button></p>`;
  },

  _wireRoomTimeline(body, gid, group) {
    const section = body.querySelector("[data-room-timeline]");
    const rid = `room:${gid}`;
    const action = (name) =>
      section.querySelector(`[data-room-label-action="${name}"]`);
    const day = action("day");
    day.onchange = () => day.value && this._changeRoomDay(rid, day.value);
    for (const [name, offset] of [
      ["previous", -1],
      ["next", 1],
    ])
      action(name).onclick = () => {
        const date = new Date(`${this._historyStateFor(rid).day}T12:00:00`);
        date.setDate(date.getDate() + offset);
        this._changeRoomDay(rid, this._toLocalInputValue(date / 1000));
      };
    action("today").onclick = () =>
      this._changeRoomDay(rid, this._toLocalInputValue(Date.now() / 1000));
    for (const button of section.querySelectorAll(
      '[data-room-label-action="edit"]',
    ))
      button.onclick = () =>
        this._action(button, async () =>
          this._selectRoomPeriod(section, gid, button),
        );
    action("new").onclick = () => this._newRoomPeriod(section, rid);
    const radarLabels = section.querySelector(
      '[data-room-action="edit-radar-labels"]',
    );
    if (radarLabels)
      radarLabels.onclick = () => {
        this._radarEditorShown.add(radarLabels.dataset.device);
        this._setSectionCollapsed(radarLabels.dataset.device, "history", false);
        this._draw();
      };
    action("apply").onclick = (event) =>
      this._action(event.currentTarget, () =>
        this._saveRoomPeriod(section, gid),
      );
    action("remove").onclick = (event) =>
      this._action(event.currentTarget, () =>
        this._removeRoomPeriod(section, gid),
      );
    this._wireHistoryRange(section, rid);
    this._wireSeenBy(section, gid);
    this._wireRoomEstimate(section, gid);
    this._wireRoomGraph(section, gid, group);
    this._wireSources(
      body,
      rid,
      { presence_sources: group.presence_sources },
      gid,
    );
  },

  _changeRoomDay(rid, value) {
    const ui = this._historyStateFor(rid);
    ui.day = value.slice(0, 10);
    ui.fields = null;
    ui.range = null;
    ui.edit = null;
    const section = this._roomGraphSection(rid.slice(5));
    if (section) this._setRoomFields(section, this._roomFieldValues(ui));
    this._drawLayout();
  },

  _setRoomFields(section, values) {
    for (const [selector, value] of [
      ['[data-action="history-start"]', values.start],
      ['[data-action="history-end"]', values.end],
      ['[data-room-label-action="state"]', values.state],
    ])
      section.querySelector(selector).value = value;
  },

  _newRoomPeriod(section, rid) {
    const ui = this._historyStateFor(rid);
    ui.range = null;
    ui.edit = null;
    this._setRoomFields(section, { start: "", end: "", state: "present" });
    this._drawLayout();
  },

  _selectRoomPeriod(section, gid, button) {
    const group = this._roomsData().groups[gid];
    const start = Number(button.dataset.start);
    const end = Number(button.dataset.end);
    const label = group.labels.find(
      (item) =>
        item.source === "manual" && item.start === start && item.end === end,
    );
    if (!label)
      throw new Error(
        "This saved period no longer exists. Refresh and select it again.",
      );
    const ui = this._historyStateFor(`room:${gid}`);
    ui.range = null;
    ui.edit = { ...label, revision: group.label_revision || 0 };
    this._setRoomFields(section, {
      start: this._historyInputValue(start),
      end: this._historyInputValue(end),
      state: label.state,
    });
    this._drawLayout();
  },

  async _saveRoomPeriod(section, gid) {
    const ui = this._historyStateFor(`room:${gid}`);
    const edit = ui.edit;
    const original = ui.range || edit;
    const start = this._historyFieldTime(section, "start", original?.start);
    const end = this._historyFieldTime(section, "end", original?.end);
    if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start)
      throw new Error(
        "Choose a start and end time, with the end after the start.",
      );
    const state = section.querySelector(
      '[data-room-label-action="state"]',
    ).value;
    if (
      !confirm(
        `${edit ? "Update this saved room period" : "Label this room range"} as ${ROOM_STATUS[state]}? Overlapping manual room labels in the chosen range will be replaced.`,
      )
    )
      return;
    if (edit)
      await this._call("edit_room_label", {
        group_id: gid,
        label_start: edit.start,
        label_end: edit.end,
        start,
        end,
        state,
        revision: edit.revision,
      });
    else
      await this._call("label_room_range", {
        group_id: gid,
        start,
        end,
        state,
      });
    await this._roomLabelsChanged(section, gid);
  },

  async _removeRoomPeriod(section, gid) {
    const edit = this._historyStateFor(`room:${gid}`).edit;
    if (
      !confirm(
        "Remove this manual room label? Sensor room labels and radar labels are kept.",
      )
    )
      return;
    await this._call("edit_room_label", {
      group_id: gid,
      label_start: edit.start,
      label_end: edit.end,
      start: edit.start,
      end: edit.end,
      state: "unlabelled",
      revision: edit.revision,
    });
    await this._roomLabelsChanged(section, gid);
  },

  async _roomLabelsChanged(section, gid) {
    this._newRoomPeriod(section, `room:${gid}`);
    await this._load(true);
  },

  _wireSeenBy(section, gid) {
    for (const chip of section.querySelectorAll("[data-seen-by]"))
      chip.onclick = () =>
        this._action(chip, async () => {
          const id = chip.dataset.seenBy;
          const episodes = this._data?.room_attribution?.[gid] || [];
          await this._call("label_history", {
            device_id: id,
            start: Number(chip.dataset.start),
            end: Number(chip.dataset.end),
            state:
              chip.getAttribute("aria-pressed") === "true"
                ? "not_present"
                : "present",
          });
          this._seenByTaps.set(`${gid}|${id}|${chip.dataset.start}`, {
            episodes: JSON.stringify(episodes),
            at: Date.now(),
          });
          const chart = this._chartState.get(id);
          if (chart) {
            chart.data = null;
            chart.cache?.clear();
          }
          await this._load(true);
        });
  },

  _wireRoomEstimate(section, gid) {
    for (const button of section.querySelectorAll("[data-room-estimate]"))
      button.onclick = () =>
        this._action(button, async () => {
          await this._call("room_estimate_feedback", {
            group_id: gid,
            correct: button.dataset.roomEstimate === "true",
          });
          await this._load(true);
        });
  },
};
