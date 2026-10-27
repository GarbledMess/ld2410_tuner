export const panelBermuda = {
  _bermudaSettings() {
    return (
      this._bermudaDraft ||
      this._data?.bermuda || { devices: [], max_distance: null, rooms: {} }
    );
  },

  _bermudaDevices() {
    const devices = new Map(
      (this._data?.bermuda_devices || []).map((device) => [
        device.entity_id,
        device,
      ]),
    );
    // Keep saved devices removable even after discovery or a draft switches them off.
    for (const entity_id of [
      ...(this._data?.bermuda?.devices || []),
      ...(this._bermudaSettings().devices || []),
    ]) {
      if (!devices.has(entity_id))
        devices.set(entity_id, {
          entity_id,
          name: entity_id,
          area: "Unavailable",
        });
    }
    return [...devices.values()];
  },

  _bermudaRoomHtml(id, group) {
    const config = this._bermudaSettings().rooms?.[id];
    const global = this._bermudaSettings().max_distance;
    const placeholder = global == null ? "Any" : `Default: ${global} m`;
    return `<div class="bermuda-row" data-bermuda-room="${this._esc(id)}">
      <label class="bermuda-toggle"><input type="checkbox" role="switch" data-bermuda-on ${config ? "checked" : ""}><span>${this._esc(group.name || id)}</span></label>
      <label class="bermuda-range"><input data-bermuda-range aria-label="Range in metres for ${this._esc(group.name || id)}" type="number" min="0" max="100" step="any" placeholder="${this._esc(placeholder)}" value="${this._esc(config?.max_distance ?? "")}" ${config ? "" : "disabled"}><span>m</span></label>
    </div>`;
  },

  _drawBermuda() {
    const section = this.shadowRoot.querySelector("#bermuda");
    section.open = !!this._bermudaOpen;
    section.ontoggle = () => {
      this._bermudaOpen = section.open;
    };
    const container = section.querySelector("[data-bermuda-content]");
    const settings = this._bermudaSettings();
    const devices = this._bermudaDevices();
    const rooms = Object.entries(this._roomsData().groups).filter(
      ([, group]) => group.kind === "area" && group.device_ids?.length,
    );
    this._updateHtml(
      container,
      `
      <p>Bermuda marks a switched-on room Present when a switched-on phone or watch is in it; it never marks a room empty.</p>
      <label class="bermuda-global">Only within <input data-bermuda-global-range aria-label="Only within (m)" type="number" min="0" max="100" step="any" placeholder="Any" value="${this._esc(settings.max_distance ?? "")}"> m</label>
      <p class="field-note">Leave blank for any distance. A blank room range uses this default.</p>
      <h3>Phones and watches</h3>
      <div class="bermuda-list">${
        devices
          .map(
            (
              device,
            ) => `<div class="bermuda-row bermuda-device" data-bermuda-device="${this._esc(device.entity_id)}">
        <div class="bermuda-device-name"><strong>${this._esc(device.name)}</strong><span class="muted">Current area: ${this._esc(device.area || "Unknown")}</span></div>
        <label class="bermuda-toggle"><input type="checkbox" role="switch" data-bermuda-on ${settings.devices.includes(device.entity_id) ? "checked" : ""}><span>Use this phone</span></label>
      </div>`,
          )
          .join("") ||
        '<p class="muted">No Bermuda tracked devices found. Area sensors need a matching range sensor.</p>'
      }</div>
      <h3>Rooms</h3>
      <div class="bermuda-list">${rooms.map(([id, group]) => this._bermudaRoomHtml(id, group)).join("") || '<p class="muted">No area rooms with radars found.</p>'}</div>
      <button data-action="bermuda-save">Save Bermuda settings</button>
      <div class="settings-action-status muted" role="status"></div>`,
    );
    const capture = () => {
      this._bermudaDraft = this._readBermuda(container);
      const global = container.querySelector(
        "[data-bermuda-global-range]",
      ).value;
      for (const row of container.querySelectorAll("[data-bermuda-room]")) {
        const range = row.querySelector("[data-bermuda-range]");
        range.disabled = !row.querySelector("[data-bermuda-on]").checked;
        range.placeholder = global === "" ? "Any" : `Default: ${global} m`;
      }
      for (const input of container.querySelectorAll("input[type=number]"))
        input.setCustomValidity("");
    };
    container.oninput = capture;
    container.onchange = capture;
    const save = container.querySelector('[data-action="bermuda-save"]');
    save.onclick = () => {
      for (const input of container.querySelectorAll(
        "input[type=number]:not(:disabled)",
      )) {
        input.setCustomValidity(
          input.value !== "" && !(Number(input.value) > 0)
            ? "Range must be greater than zero."
            : "",
        );
        if (!input.reportValidity()) return;
      }
      const settings = this._readBermuda(container);
      this._bermudaDraft = settings;
      return this._action(save, async () => {
        await this._call("configure_bermuda", { settings });
        this._bermudaDraft = null;
        await this._load(true);
      });
    };
  },

  _readBermuda(container) {
    const distance = (input) =>
      input.value === "" ? null : Number(input.value);
    const devices = [...container.querySelectorAll("[data-bermuda-device]")]
      .filter((row) => row.querySelector("[data-bermuda-on]").checked)
      .map((row) => row.dataset.bermudaDevice);
    const rooms = Object.fromEntries(
      [...container.querySelectorAll("[data-bermuda-room]")]
        .filter((row) => row.querySelector("[data-bermuda-on]").checked)
        .map((row) => [
          row.dataset.bermudaRoom,
          { max_distance: distance(row.querySelector("[data-bermuda-range]")) },
        ]),
    );
    return {
      devices,
      max_distance: distance(
        container.querySelector("[data-bermuda-global-range]"),
      ),
      rooms,
    };
  },
};
