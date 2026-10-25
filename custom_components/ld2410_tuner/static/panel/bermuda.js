export const panelBermuda = {
  _bermudaSettings() {
    return this._bermudaDraft || this._data?.bermuda || { devices: {} };
  },

  _bermudaDevices() {
    const devices = new Map(
      (this._data?.bermuda_devices || []).map((device) => [
        device.entity_id,
        device,
      ]),
    );
    // Saved assignments remain removable while a tracked device is unavailable.
    for (const entity_id of Object.keys(this._bermudaSettings().devices))
      if (!devices.has(entity_id))
        devices.set(entity_id, {
          entity_id,
          name: entity_id,
          area: "Unavailable",
        });
    return [...devices.values()];
  },

  _bermudaRadarHtml(entityId, id, radar) {
    const config = this._bermudaSettings().devices[entityId]?.radars[id];
    const info = this._data?.bermuda_radars?.[id];
    const reason =
      info?.reason ||
      (!(info?.area_id || radar.area_id)
        ? "Assign this radar a Home Assistant area to use Bermuda."
        : "");
    const areaId = info?.area_id || radar.area_id;
    const areaName = this._roomsData().areas[areaId] || areaId;
    const radarName = radar.name || id;
    const label = areaName ? `${radarName} · ${areaName}` : radarName;
    const reasonId = `bermuda-reason-${Array.from(entityId + ":" + id, (c) => c.codePointAt(0).toString(16)).join("-")}`;
    return `<div class="bermuda-radar" data-bermuda-radar="${this._esc(id)}">
      <label class="source-negative"><input type="checkbox" data-bermuda-on ${config ? "checked" : ""} ${reason ? `disabled aria-describedby="${reasonId}"` : ""}><span>${this._esc(label)}</span></label>
      ${!reason ? `<label class="bermuda-range" ${config ? "" : "hidden"}>Range (m)<input data-bermuda-range aria-label="Range (m) for ${this._esc(label)}" type="number" min="0" max="100" step="any" placeholder="Any" value="${this._esc(config?.max_distance ?? "")}" ${config ? "" : "disabled"}></label>` : ""}
      ${reason ? `<p class="field-note" id="${reasonId}">${this._esc(reason)}</p>` : ""}
    </div>`;
  },

  _drawBermuda() {
    const section = this.shadowRoot.querySelector("#bermuda");
    section.open = !!this._bermudaOpen;
    section.ontoggle = () => {
      this._bermudaOpen = section.open;
    };
    const container = section.querySelector("[data-bermuda-content]");
    const devices = this._bermudaDevices();
    this._updateHtml(
      container,
      `<p>Choose which radars each tracked phone or watch labels. Bermuda must place it in the radar’s own Home Assistant area. Blank range accepts any distance. Each radar’s presence-source confidence, absence and trim settings apply.</p>
      ${
        devices
          .map(
            (device) =>
              `<fieldset class="source-row" data-bermuda-device="${this._esc(device.entity_id)}"><legend>${this._esc(device.name)}</legend><p class="muted">Current area: ${this._esc(device.area || "Unknown")} · ${this._esc(device.entity_id)}</p><div class="bermuda-radars">${Object.entries(
                this._data?.devices || {},
              )
                .map(([id, radar]) =>
                  this._bermudaRadarHtml(device.entity_id, id, radar),
                )
                .join("")}</div></fieldset>`,
          )
          .join("") ||
        "<p>No Bermuda tracked devices found. Area sensors need a matching range sensor.</p>"
      }
      <button data-action="bermuda-save">Save Bermuda settings</button>
      <div class="settings-action-status muted" role="status"></div>`,
    );
    container.oninput = () => {
      this._bermudaDraft = this._readBermuda(container);
    };
    container.onchange = (event) => {
      const row = event.target.closest("[data-bermuda-radar]");
      if (event.target.matches("[data-bermuda-on]")) {
        row.querySelector(".bermuda-range").hidden = !event.target.checked;
        row.querySelector("[data-bermuda-range]").disabled =
          !event.target.checked;
      }
      this._bermudaDraft = this._readBermuda(container);
    };
    const save = container.querySelector('[data-action="bermuda-save"]');
    save.onclick = () => {
      const inputs = [
        ...container.querySelectorAll("[data-bermuda-range]:not(:disabled)"),
      ];
      for (const input of inputs) {
        input.setCustomValidity(
          input.value !== "" && !(Number(input.value) > 0)
            ? "Range must be greater than zero."
            : "",
        );
        if (!input.reportValidity()) return;
      }
      const settings = this._readBermuda(container);
      return this._action(save, async () => {
        await this._call("configure_bermuda", { settings });
        this._bermudaDraft = null;
        await this._load(true);
      });
    };
  },

  _readBermuda(container) {
    const settings = structuredClone(this._bermudaSettings());
    for (const row of container.querySelectorAll("[data-bermuda-device]")) {
      const entityId = row.dataset.bermudaDevice;
      const radars = { ...settings.devices[entityId]?.radars };
      for (const radar of row.querySelectorAll("[data-bermuda-radar]")) {
        const id = radar.dataset.bermudaRadar;
        const on = radar.querySelector("[data-bermuda-on]");
        // Preserve an assignment when its radar temporarily has no area.
        if (on.disabled) continue;
        if (!on.checked) delete radars[id];
        else {
          const value = radar.querySelector("[data-bermuda-range]").value;
          radars[id] = { max_distance: value === "" ? null : Number(value) };
        }
      }
      if (Object.keys(radars).length) settings.devices[entityId] = { radars };
      else delete settings.devices[entityId];
    }
    return settings;
  },
};
