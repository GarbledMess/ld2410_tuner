// "Label several rooms and radars": one period labelled on any mix of rooms, zones and radars
// in one request, each at its own level (a room's Empty is Empty for each of its radars; a
// radar's own label means "this radar should have detected here"). The first time every
// room is ticked, with any recording radar in no area; afterwards the last selection (kept by the
// integration, so every browser sees it). Named selections can be saved and applied.

const byName = (a, b) =>
  a.name.localeCompare(b.name) || a.id.localeCompare(b.id);

import { STATUS_NAMES } from "./history.js";

export const panelBulkLabels = {
  // Rooms (groups without a parent), each with its zones and radars, then areas without a
  // room (only radars that record nothing: a radar joins its area's room once it records),
  // then radars in no area. Every radar is listed where Home Assistant places it.
  _bulkTree() {
    const devices = this._data?.devices || {};
    const rooms = this._roomsData();
    const members = rooms.members || {};
    const radar = (id) => ({
      id,
      name: devices[id]?.name || id,
      recording: devices[id]?.recording_enabled !== false,
    });
    const entries = Object.entries(rooms.groups).map(([id, g]) => ({
      id,
      ...g,
    }));
    const areaRoom = (areaId) =>
      entries.find((room) => room.kind === "area" && room.area_id === areaId);
    const isZone = (g) => g.kind === "zone" && !!areaRoom(g.area_id);
    const placed = new Set();
    const nodes = entries
      .filter((g) => !isZone(g))
      .map((room) => {
        const zones = entries.filter(
          (g) =>
            isZone(g) && room.kind === "area" && g.area_id === room.area_id,
        );
        const ids = new Set(
          [room, ...zones].flatMap((g) => g.device_ids || []),
        );
        if (room.kind === "area")
          for (const [id, member] of Object.entries(members))
            if (member.area_id === room.area_id) ids.add(id);
        return {
          id: room.id,
          name: room.name || room.id,
          zones: zones
            .map((g) => ({ id: g.id, name: g.name || g.id }))
            .sort(byName),
          ids,
        };
      });
    const roomless = new Map();
    for (const [id, member] of Object.entries(members))
      if (member.area_id && !areaRoom(member.area_id)) {
        if (!roomless.has(member.area_id))
          roomless.set(member.area_id, {
            id: null,
            area: member.area_id,
            name: rooms.areas?.[member.area_id] || member.area_id,
            zones: [],
            ids: new Set(),
          });
        roomless.get(member.area_id).ids.add(id);
      }
    const finish = (node) => {
      const radars = [...node.ids].filter(
        (id) => devices[id] && !placed.has(id),
      );
      radars.forEach((id) => placed.add(id));
      return { ...node, radars: radars.map(radar).sort(byName) };
    };
    const tree = [...nodes].sort(byName).map(finish);
    const areas = [...roomless.values()].sort(byName).map(finish);
    const loose = Object.keys(devices)
      .filter((id) => !placed.has(id))
      .map(radar)
      .sort(byName);
    return { rooms: tree, areas, loose };
  },

  // The card's own state survives every poll's redraw: the period, the name and, once
  // chosen, what is ticked. Until then it follows the saved last selection, or every room
  // and every radar in no room.
  _bulkState(tree) {
    if (!this._bulkDraft) {
      const now = Math.floor(Date.now() / 1000 / 60) * 60;
      this._bulkDraft = {
        chosen: null,
        from: this._toLocalInputValue(now - 3600),
        to: this._toLocalInputValue(now),
        name: "",
        template: "",
        state: "present",
        status: "",
      };
    }
    const draft = this._bulkDraft;
    const known = {
      group_ids: tree.rooms.flatMap((room) => [
        room.id,
        ...room.zones.map((zone) => zone.id),
      ]),
      device_ids: [
        ...[...tree.rooms, ...tree.areas].flatMap((node) =>
          node.radars.map((radar) => radar.id),
        ),
        ...tree.loose.map((radar) => radar.id),
      ],
    };
    const wanted = draft.chosen ??
      this._data?.label_selections?.last ?? {
        group_ids: tree.rooms.map((room) => room.id),
        device_ids: tree.loose
          .filter((radar) => radar.recording)
          .map((radar) => radar.id),
      };
    draft.groups = new Set(
      (wanted.group_ids || []).filter((id) => known.group_ids.includes(id)),
    );
    draft.radars = new Set(
      (wanted.device_ids || []).filter((id) => known.device_ids.includes(id)),
    );
    return draft;
  },

  _bulkBox(kind, id, name, checked) {
    const attribute = kind === "group" ? "data-bulk-group" : "data-bulk-radar";
    return `<label class="bulk-item"><input type="checkbox" ${attribute}="${this._esc(id)}" ${checked ? "checked" : ""}> ${this._esc(name)}</label>`;
  },

  _bulkRadarsHtml(radars, draft) {
    return radars
      .map((radar) =>
        this._bulkBox(
          "radar",
          radar.id,
          radar.recording ? radar.name : `${radar.name} (not recording)`,
          draft.radars.has(radar.id),
        ),
      )
      .join("");
  },

  _bulkTreeHtml(tree, draft) {
    const rooms = tree.rooms
      .map(
        (room) =>
          `<div class="bulk-room" data-bulk-node="${this._esc(room.id)}">${this._bulkBox("group", room.id, room.name, draft.groups.has(room.id))}<div class="bulk-children">${room.zones
            .map((zone) =>
              this._bulkBox(
                "group",
                zone.id,
                `Zone: ${zone.name}`,
                draft.groups.has(zone.id),
              ),
            )
            .join("")}${this._bulkRadarsHtml(room.radars, draft)}</div></div>`,
      )
      .join("");
    // An area whose radars record nothing has no room to label: its radars only.
    const areas = tree.areas
      .map(
        (area) =>
          `<div class="bulk-room" data-bulk-area="${this._esc(area.area)}"><span class="muted">${this._esc(area.name)}</span><div class="bulk-children">${this._bulkRadarsHtml(area.radars, draft)}</div></div>`,
      )
      .join("");
    const loose = tree.loose.length
      ? `<div class="bulk-room"><span class="muted">Not in an area</span><div class="bulk-children">${this._bulkRadarsHtml(tree.loose, draft)}</div></div>`
      : "";
    return rooms + areas + loose;
  },

  _drawBulkLabels() {
    const container = this.shadowRoot.querySelector("[data-bulk-content]");
    if (!container) return;
    const tree = this._bulkTree();
    const draft = this._bulkState(tree);
    const templates = this._data?.label_selections?.templates || [];
    const options = templates
      .map(
        (t) =>
          `<option value="${this._esc(t.name)}" ${draft.template === t.name ? "selected" : ""}>${this._esc(t.name)}</option>`,
      )
      .join("");
    const summary = this._bulkCountText(draft);
    this._updateHtml(
      container,
      `<section class="bulk-step">
        <div class="bulk-step-head">
          <h3>Rooms and radars</h3>
          <div class="bulk-quick"><button type="button" class="secondary" data-action="bulk-all">All rooms</button><button type="button" class="secondary" data-action="bulk-none">Clear</button></div>
        </div>
        <p class="field-note">A room's label covers its radars: Empty counts for each of them, Present for the radar that saw it. Tick a radar on its own only to correct that radar.</p>
        <div class="bulk-tree" role="group" aria-label="Rooms, zones and radars">${this._bulkTreeHtml(tree, draft)}</div>
        <div class="bulk-selections">
          <div class="bulk-saved">
            <label>Saved selection<select data-role="bulk-template"><option value="">Choose…</option>${options}</select></label>
            <button type="button" class="secondary" data-action="bulk-delete" ${draft.template ? "" : "disabled"}>Delete</button>
          </div>
          <div class="bulk-save">
            <label>Save this selection as<input type="text" data-role="bulk-name" maxlength="60" value="${this._esc(draft.name)}" placeholder="e.g. Upstairs"></label>
            <button type="button" class="secondary" data-action="bulk-save">Save</button>
          </div>
        </div>
      </section>
      <section class="bulk-step">
        <h3>Period</h3>
        <div class="history-fields">
          <label>From<input type="datetime-local" step="1" data-role="bulk-from" value="${this._esc(draft.from)}"></label>
          <label>To<input type="datetime-local" step="1" data-role="bulk-to" value="${this._esc(draft.to)}"></label>
          <label>Status<select data-role="bulk-state">${Object.entries(
            STATUS_NAMES,
          )
            .map(
              ([state, name]) =>
                `<option value="${state}" ${draft.state === state ? "selected" : ""}>${name}</option>`,
            )
            .join("")}</select></label>
        </div>
        <p class="field-note bulk-count">${this._esc(summary)}</p>
        <div class="history-buttons"><button type="button" class="primary" data-action="bulk-apply">Label time range</button></div>
        <div class="settings-action-status muted" role="status">${this._esc(draft.status)}</div>
      </section>`,
    );
    this._wireBulkLabels(container, draft, tree, templates);
  },

  _bulkCountText(draft) {
    const rooms = draft.groups.size;
    const radars = draft.radars.size;
    if (!rooms && !radars) return "Nothing ticked yet.";
    const parts = [
      rooms &&
        `${rooms} room${rooms === 1 ? "" : "s"} or zone${rooms === 1 ? "" : "s"}`,
      radars && `${radars} radar${radars === 1 ? "" : "s"}`,
    ].filter(Boolean);
    return `Labels ${parts.join(" and ")}.`;
  },

  _bulkChosen(draft) {
    return { device_ids: [...draft.radars], group_ids: [...draft.groups] };
  },

  _wireBulkLabels(container, draft, tree, templates) {
    const redraw = (status = "") => {
      draft.status = status;
      this._drawBulkLabels();
    };
    const pick = (chosen) => {
      draft.chosen = chosen;
      redraw();
    };
    for (const box of container.querySelectorAll(
      "[data-bulk-group],[data-bulk-radar]",
    ))
      box.onchange = () => {
        const [set, id] = box.dataset.bulkGroup
          ? [draft.groups, box.dataset.bulkGroup]
          : [draft.radars, box.dataset.bulkRadar];
        if (box.checked) set.add(id);
        else set.delete(id);
        draft.template = "";
        pick(this._bulkChosen(draft));
      };
    container.querySelector('[data-action="bulk-all"]').onclick = () => {
      draft.template = "";
      pick({
        group_ids: tree.rooms.map((room) => room.id),
        device_ids: tree.loose
          .filter((radar) => radar.recording)
          .map((radar) => radar.id),
      });
    };
    container.querySelector('[data-action="bulk-none"]').onclick = () => {
      draft.template = "";
      pick({ group_ids: [], device_ids: [] });
    };
    for (const [role, key] of [
      ["bulk-from", "from"],
      ["bulk-to", "to"],
      ["bulk-name", "name"],
    ])
      container.querySelector(`[data-role="${role}"]`).oninput = (event) => {
        draft[key] = event.target.value;
      };
    const template = container.querySelector('[data-role="bulk-template"]');
    template.onchange = () => {
      draft.template = template.value;
      const found = templates.find((t) => t.name === template.value);
      if (found)
        pick({ group_ids: found.group_ids, device_ids: found.device_ids });
      else redraw();
    };
    this._wireBulkSaving(container, draft, redraw);
    const state = container.querySelector('[data-role="bulk-state"]');
    state.onchange = () => {
      draft.state = state.value;
    };
    const apply = container.querySelector('[data-action="bulk-apply"]');
    apply.onclick = () =>
      this._action(apply, () => this._bulkLabel(draft, draft.state, redraw));
  },

  _wireBulkSaving(container, draft, redraw) {
    const save = container.querySelector('[data-action="bulk-save"]');
    save.onclick = () =>
      this._action(save, async () => {
        const name = draft.name.trim();
        if (!name) return redraw("Give the selection a name to save it.");
        if (!draft.groups.size && !draft.radars.size)
          return redraw("Tick at least one room, zone or radar to save.");
        await this._call("save_label_template", {
          name,
          ...this._bulkChosen(draft),
        });
        draft.name = "";
        draft.template = name;
        await this._load(true);
        redraw(`Saved "${name}".`);
      });
    const remove = container.querySelector('[data-action="bulk-delete"]');
    remove.onclick = () =>
      this._action(remove, async () => {
        const name = draft.template;
        await this._call("delete_label_template", { name });
        draft.template = "";
        await this._load(true);
        redraw(`Deleted "${name}".`);
      });
  },

  async _bulkLabel(draft, state, redraw) {
    const start = new Date(draft.from).getTime() / 1000;
    const end = new Date(draft.to).getTime() / 1000;
    if (!Number.isFinite(start) || !Number.isFinite(end))
      return redraw("Choose when the period starts and ends.");
    if (end <= start) return redraw("The end must be after the start.");
    if (!draft.groups.size && !draft.radars.size)
      return redraw("Tick at least one room, zone or radar.");
    const found = await this._call("label_several", {
      ...this._bulkChosen(draft),
      start,
      end,
      state,
    });
    const labelled = found?.labelled || {};
    const rooms = (labelled.group_ids || []).length;
    const radars = (labelled.device_ids || []).length;
    const parts = [
      rooms &&
        `${rooms} room${rooms === 1 ? "" : "s"} or zone${rooms === 1 ? "" : "s"}`,
      radars && `${radars} radar${radars === 1 ? "" : "s"}`,
    ].filter(Boolean);
    const label = STATUS_NAMES[state];
    await this._load(true);
    redraw(`Labelled ${parts.join(" and ")} ${label}.`);
  },
};
