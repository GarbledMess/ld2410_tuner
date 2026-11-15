// "Label several rooms and radars": one period labelled on any mix of rooms, zones and radars
// in one request, each at its own level (a room's Empty is Empty for each of its radars; a
// radar's own label means "this radar should have detected here"). The first time every
// room is ticked, with any radar in no room; afterwards the last selection (kept by the
// integration, so every browser sees it). Named selections can be saved and applied.
const STATES = [
  ["present", "Present"],
  ["not_present", "Not present"],
  ["unknown", "Unknown"],
];

const byName = (a, b) =>
  a.name.localeCompare(b.name) || a.id.localeCompare(b.id);

export const panelBulkLabels = {
  // Rooms (groups without a parent), each with its zones and radars; then radars in no room.
  _bulkTree() {
    const devices = this._data?.devices || {};
    const groups = this._roomsData().groups;
    const radar = (id) => ({ id, name: devices[id]?.name || id });
    const entries = Object.entries(groups).map(([id, g]) => ({ id, ...g }));
    const isZone = (g) =>
      g.kind === "zone" &&
      entries.some(
        (room) => room.kind === "area" && room.area_id === g.area_id,
      );
    const rooms = entries
      .filter((g) => !isZone(g))
      .map((room) => {
        const zones = entries.filter(
          (g) =>
            isZone(g) && room.kind === "area" && g.area_id === room.area_id,
        );
        const ids = new Set(
          [room, ...zones].flatMap((g) => g.device_ids || []),
        );
        return {
          id: room.id,
          name: room.name || room.id,
          zones: zones
            .map((g) => ({ id: g.id, name: g.name || g.id }))
            .sort(byName),
          radars: [...ids]
            .filter((id) => devices[id])
            .map(radar)
            .sort(byName),
        };
      })
      .sort(byName);
    const placed = new Set(entries.flatMap((g) => g.device_ids || []));
    const loose = Object.keys(devices)
      .filter((id) => !placed.has(id))
      .map(radar)
      .sort(byName);
    return { rooms, loose };
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
        ...tree.rooms.flatMap((room) => room.radars.map((radar) => radar.id)),
        ...tree.loose.map((radar) => radar.id),
      ],
    };
    const wanted = draft.chosen ??
      this._data?.label_selections?.last ?? {
        group_ids: tree.rooms.map((room) => room.id),
        device_ids: tree.loose.map((radar) => radar.id),
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
            .join("")}${room.radars
            .map((radar) =>
              this._bulkBox(
                "radar",
                radar.id,
                radar.name,
                draft.radars.has(radar.id),
              ),
            )
            .join("")}</div></div>`,
      )
      .join("");
    const loose = tree.loose.length
      ? `<div class="bulk-room"><span class="muted">Not in a room</span><div class="bulk-children">${tree.loose
          .map((radar) =>
            this._bulkBox(
              "radar",
              radar.id,
              radar.name,
              draft.radars.has(radar.id),
            ),
          )
          .join("")}</div></div>`
      : "";
    return rooms + loose;
  },

  _drawBulkLabels() {
    const container = this.shadowRoot.querySelector("[data-bulk-content]");
    if (!container) return;
    const tree = this._bulkTree();
    const draft = this._bulkState(tree);
    const templates = this._data?.label_selections?.templates || [];
    const count = draft.groups.size + draft.radars.size;
    const options = templates
      .map(
        (t) =>
          `<option value="${this._esc(t.name)}" ${draft.template === t.name ? "selected" : ""}>${this._esc(t.name)}</option>`,
      )
      .join("");
    this._updateHtml(
      container,
      `<p class="muted">A room or zone label covers its radars: Empty is Empty for each of them, and Present counts for the radar that saw it. Tick a radar itself only when that radar should or should not have detected.</p>
      <div class="storage-fields">
        <label>From<input type="datetime-local" data-role="bulk-from" value="${this._esc(draft.from)}"></label>
        <label>To<input type="datetime-local" data-role="bulk-to" value="${this._esc(draft.to)}"></label>
      </div>
      <fieldset class="bulk-radars"><legend>Rooms, zones and radars (${count} ticked)</legend>
        <div class="bulk-actions"><button type="button" class="secondary" data-action="bulk-all">All rooms</button><button type="button" class="secondary" data-action="bulk-none">Clear</button></div>
        <div class="bulk-tree">${this._bulkTreeHtml(tree, draft)}</div>
      </fieldset>
      <div class="bulk-templates">
        <label>Saved selection<select data-role="bulk-template"><option value="">Choose…</option>${options}</select></label>
        <button type="button" class="secondary" data-action="bulk-delete" ${draft.template ? "" : "disabled"}>Delete</button>
        <label>Name<input type="text" data-role="bulk-name" maxlength="60" value="${this._esc(draft.name)}" placeholder="e.g. Upstairs"></label>
        <button type="button" class="secondary" data-action="bulk-save">Save selection</button>
      </div>
      <div class="bulk-actions">${STATES.map(([state, label]) => `<button type="button" data-bulk-state="${state}">${label}</button>`).join("")}</div>
      <div class="settings-action-status muted" role="status">${this._esc(draft.status)}</div>`,
    );
    this._wireBulkLabels(container, draft, tree, templates);
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
        device_ids: tree.loose.map((radar) => radar.id),
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
    for (const button of container.querySelectorAll("[data-bulk-state]"))
      button.onclick = () =>
        this._action(button, () =>
          this._bulkLabel(draft, button.dataset.bulkState, redraw),
        );
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
    const label = STATES.find(([value]) => value === state)[1];
    await this._load(true);
    redraw(`Labelled ${parts.join(" and ")} ${label}.`);
  },
};
