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

  _bermudaDefaultArea(kind, id, target) {
    if (kind === "radars")
      return this._data?.bermuda_radars?.[id]?.area_id || target.area_id;
    if (target.area_id) return target.area_id;
    return [
      ...new Set(
        (target.device_ids || [])
          .map(
            (id) =>
              this._data?.bermuda_radars?.[id]?.area_id ||
              this._roomsData().members[id]?.area_id ||
              this._data?.devices[id]?.area_id,
          )
          .filter(Boolean),
      ),
    ];
  },

  _bermudaRadars() {
    const stored = Object.fromEntries(
      Object.entries(this._data?.bermuda_radars || {}).map(([id, info]) => [
        id,
        { name: id, area_id: info.area_id },
      ]),
    );
    return { ...stored, ...this._data?.devices };
  },

  _bermudaRadarHtml(entityId, id, target, kind = "radars") {
    const config = this._bermudaSettings().devices[entityId]?.[kind]?.[id];
    const own = this._bermudaDefaultArea(kind, id, target);
    const defaultAreas = (Array.isArray(own) ? own : [own]).filter(Boolean);
    const area = config?.area || defaultAreas;
    const areas = this._roomsData().areas;
    const areaNames = (Array.isArray(area) ? area : [area]).map(
      (id) => areas[id] || `${id} (unknown area)`,
    );
    const label = [target.name || id, areaNames.join(" / ")]
      .filter(Boolean)
      .join(" · ");
    const reason =
      !defaultAreas.length && !config?.area
        ? "Assign a Home Assistant area or choose an Area override to enable this target."
        : "";
    const reasonId = `bermuda-reason-${Array.from(entityId + ":" + kind + ":" + id, (c) => c.codePointAt(0).toString(16)).join("-")}`;
    const picker = this._sourceAreaHtml(config?.area || "", "Own area")
      .replace("data-source-area required", "data-bermuda-area")
      .replace("Target area", "Area");
    return `<div class="bermuda-radar" data-bermuda-own-area="${!!defaultAreas.length}" data-bermuda-kind="${kind}" data-bermuda-${kind === "radars" ? "radar" : "room"}="${this._esc(id)}">
      <label class="source-negative"><input type="checkbox" data-bermuda-on ${config ? "checked" : ""} ${reason && !config ? `disabled aria-describedby="${reasonId}"` : ""}><span>${this._esc(label)}</span></label>
      <label class="bermuda-range" ${config ? "" : "hidden"}>Range (m)<input data-bermuda-range aria-label="Range (m) for ${this._esc(label)}" type="number" min="0" max="100" step="any" placeholder="Any" value="${this._esc(config?.max_distance ?? "")}" ${config ? "" : "disabled"}></label>
      <span class="bermuda-area" ${config || !defaultAreas.length ? "" : "hidden"}>${picker}</span>
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
      `<p>Choose which radars, rooms and zones each tracked phone or watch labels. Own area uses the target’s area, or any member radar’s area for a zone without an area. Override it with Area. Blank range accepts any distance. Each target’s presence-source confidence, absence and trim settings apply.</p>
      ${
        devices
          .map(
            (device) =>
              `<fieldset class="source-row" data-bermuda-device="${this._esc(device.entity_id)}"><legend>${this._esc(device.name)}</legend><p class="muted">Current area: ${this._esc(device.area || "Unknown")} · ${this._esc(device.entity_id)}</p><h4>Radars</h4><div class="bermuda-radars">${Object.entries(
                this._bermudaRadars(),
              )
                .map(([id, radar]) =>
                  this._bermudaRadarHtml(device.entity_id, id, radar),
                )
                .join("")}</div><h4>Rooms</h4><div class="bermuda-radars">${
                Object.entries(this._roomsData().groups)
                  .map(([id, group]) =>
                    this._bermudaRadarHtml(
                      device.entity_id,
                      id,
                      group,
                      "rooms",
                    ),
                  )
                  .join("") || "<p>No rooms or zones defined.</p>"
              }</div></fieldset>`,
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
      const row = event.target.closest("[data-bermuda-kind]");
      if (!row) return;
      const on = row.querySelector("[data-bermuda-on]");
      const ownArea = row.dataset.bermudaOwnArea === "true";
      const missingArea =
        !ownArea && !row.querySelector("[data-bermuda-area]").value;
      if (event.target.matches("[data-bermuda-area]") && missingArea)
        on.checked = false;
      on.disabled = missingArea && !on.checked;
      row.querySelector(".bermuda-range").hidden = !on.checked;
      row.querySelector("[data-bermuda-range]").disabled = !on.checked;
      row.querySelector(".bermuda-area").hidden = !on.checked && ownArea;
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
      const device = {
        radars: { ...settings.devices[entityId]?.radars },
        rooms: { ...settings.devices[entityId]?.rooms },
      };
      for (const target of row.querySelectorAll("[data-bermuda-kind]")) {
        const kind = target.dataset.bermudaKind;
        const id =
          kind === "radars"
            ? target.dataset.bermudaRadar
            : target.dataset.bermudaRoom;
        const on = target.querySelector("[data-bermuda-on]");
        if (!on.checked) delete device[kind][id];
        else {
          const value = target.querySelector("[data-bermuda-range]").value;
          device[kind][id] = {
            max_distance: value === "" ? null : Number(value),
            area: target.querySelector("[data-bermuda-area]").value || null,
          };
        }
      }
      if (Object.keys(device.radars).length || Object.keys(device.rooms).length)
        settings.devices[entityId] = device;
      else delete settings.devices[entityId];
    }
    return settings;
  },
};
