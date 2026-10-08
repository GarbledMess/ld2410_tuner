export const panelRooms = {
  _drawRooms() {
    const container = this.shadowRoot.querySelector("#rooms");
    const rooms = this._data?.rooms || { areas: {}, members: {}, groups: {} };
    const entries = Object.entries(rooms.groups).sort((a, b) =>
      `${a[1].area_id || "~"}:${a[1].kind === "area" ? "0" : "1"}:${a[1].name}`.localeCompare(
        `${b[1].area_id || "~"}:${b[1].kind === "area" ? "0" : "1"}:${b[1].name}`,
      ),
    );
    this._updateHtml(
      container.querySelector(".room-list"),
      entries.map(([id, group]) => this._roomHtml(id, group, rooms)).join("") ||
        '<p class="muted">Assign recording radars to Home Assistant areas, or create a group below.</p>',
    );
    this._updateHtml(
      container.querySelector(".room-editor"),
      this._roomEditorHtml(rooms),
    );
    container.querySelector('[data-room-action="add"]').onclick = () => {
      this._roomDraft = { name: "", area_id: "", device_ids: [] };
      this._drawRooms();
    };
    for (const section of container.querySelectorAll("[data-room-id]"))
      this._wireRoom(section, rooms);
    this._wireRoomEditor(container);
  },

  _roomHtml(id, group, rooms) {
    const report = group.assessment || {};
    const busy = report.state === "running";
    const names = group.device_ids
      .map((key) => rooms.members[key]?.name || "Unavailable radar")
      .join(", ");
    const area =
      rooms.areas[group.area_id] ||
      (group.area_id ? "Area removed" : "Manual group");
    const kind = group.kind === "area" ? "Whole area" : "Independent zone";
    const marker = this._roomAssessmentMarker(report);
    const reason =
      report.reason || (report.state === "pending" ? "No assessment yet" : "");
    const status = busy
      ? "Assessing recorded coverage. You can leave and return."
      : reason;
    const removeLabel =
      group.kind === "area" ? "Restore area default" : "Remove group";
    const removeButton = group.automatic
      ? ""
      : `<button data-room-action="remove">${removeLabel}</button>`;
    return `<details class="pattern-comparison" data-room-id="${this._esc(id)}" ${this._roomOpen?.has(id) ? "open" : ""}>
      <summary><b>${this._esc(group.name)}</b> · ${kind} · ${this._scoreText(report.room)}${marker}</summary>
      <p class="muted">${this._esc(area)} · ${this._esc(names)}${group.automatic ? " · Uses recording radars in this area" : " · Manual membership"}</p>
      <div class="chart-controls"><label>History <select data-room-hours><option value="24">24 hours</option><option value="72">3 days</option><option value="168">7 days</option></select></label>
      <button data-room-action="assess" ${busy ? 'disabled class="is-busy"' : ""}>Assess current settings</button>
      <button data-room-action="edit">Edit members</button>${removeButton}</div>
      <div role="status" class="muted ${busy ? "is-busy" : ""}">${this._esc(status)}</div>
      ${this._roomReportHtml(report, rooms.members)}${this._roomLearningHtml(id, group, rooms)}</details>`;
  },

  _roomAssessmentMarker(report) {
    if (report.state === "running") return " · Assessing…";
    if (report.state === "stale") return " · Needs reassessment";
    return "";
  },

  _wireRoom(section, rooms) {
    const id = section.dataset.roomId;
    const group = rooms.groups[id];
    this._wireRoomLearning(section, id, group);
    const hours = section.querySelector("[data-room-hours]");
    this._roomHours ||= new Map();
    hours.value = this._roomHours.get(id) || "24";
    hours.onchange = () => this._roomHours.set(id, hours.value);
    section.ontoggle = () => {
      this._roomOpen ||= new Set();
      if (section.open) this._roomOpen.add(id);
      else this._roomOpen.delete(id);
    };
    section.querySelector('[data-room-action="edit"]').onclick = () => {
      this._roomDraft = {
        group_id: id,
        name: group.name,
        area_id: group.area_id || "",
        device_ids: [...group.device_ids],
      };
      this._drawRooms();
      this.shadowRoot.querySelector(".room-editor input").focus();
    };
    for (const action of ["assess", "remove"]) {
      const button = section.querySelector(`[data-room-action="${action}"]`);
      if (button)
        button.onclick = () =>
          this._action(button, async () => {
            await this._call(
              action === "assess" ? "assess_room" : "remove_room",
              {
                group_id: id,
                ...(action === "assess" ? { hours: Number(hours.value) } : {}),
              },
            );
            await this._load(true);
          });
    }
  },

  _roomEditorHtml(rooms) {
    const draft = this._roomDraft;
    if (!draft) return "";
    const areaOptions = Object.entries(rooms.areas)
      .map(
        ([id, name]) =>
          `<option value="${this._esc(id)}" ${draft.area_id === id ? "selected" : ""}>${this._esc(name)}</option>`,
      )
      .join("");
    const members = Object.entries(rooms.members)
      .map(
        ([id, item]) =>
          `<label><input type="checkbox" data-room-member="${this._esc(id)}" ${draft.device_ids.includes(id) ? "checked" : ""}> ${this._esc(item.name)} <span class="muted">${this._esc(rooms.areas[item.area_id] || "No area")}${item.recording_enabled ? "" : " · recording paused"}</span></label>`,
      )
      .join("");
    return `<form class="pattern-comparison"><h3>${draft.group_id ? "Edit group" : "Add independent zone or manual group"}</h3>
      <div class="history-fields"><label>Name<input name="room-name" required maxlength="80" value="${this._esc(draft.name)}"></label>
      <label>Area<select name="room-area" ${draft.group_id?.startsWith("area:") ? "disabled" : ""}><option value="">No area / custom group</option>${areaOptions}</select></label></div>
      <fieldset class="room-members"><legend>Radars covering this zone</legend>${members}</fieldset>
      <p class="muted">Select only radars that can represent this zone. A radar shared with another zone contributes to both; grouping cannot distinguish which side of its coverage triggered. Assigning an area here organizes this group; it does not move devices in Home Assistant or change whole-area membership.</p>
      <button type="submit">Save group</button> <button type="button" data-room-action="cancel">Cancel</button></form>`;
  },

  _wireRoomEditor(container) {
    const form = container.querySelector(".room-editor form");
    if (!form) return;
    const capture = () => {
      this._roomDraft = {
        ...this._roomDraft,
        name: form.elements["room-name"].value,
        area_id: form.elements["room-area"].value,
        device_ids: [
          ...form.querySelectorAll("[data-room-member]:checked"),
        ].map((input) => input.dataset.roomMember),
      };
    };
    form.oninput = capture;
    form.onchange = capture;
    form.querySelector('[data-room-action="cancel"]').onclick = () => {
      this._roomDraft = null;
      this._drawRooms();
    };
    form.onsubmit = (event) => {
      event.preventDefault();
      capture();
      const button = form.querySelector('[type="submit"]');
      void this._action(button, async () => {
        await this._call("configure_room", {
          ...this._roomDraft,
          area_id: this._roomDraft.area_id || null,
        });
        this._roomDraft = null;
        await this._load(true);
      });
    };
  },
};
