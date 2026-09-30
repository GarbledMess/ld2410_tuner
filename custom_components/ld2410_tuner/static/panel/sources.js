export const panelSources = {
  _sourcesHtml(id, d) {
    const config = this._sourceDrafts?.get(id) || d.presence_sources || {};
    const current = d.presence_sources?.state || "unknown";
    const status = d.presence_sources?.buffering
      ? "Buffering presence: waiting for a usable interval after both trims."
      : current === "unknown"
        ? "No external label; radar guessing continues."
        : `External label: ${current === "present" ? "Present" : "Not Present"}.`;
    return `<details class="presence-sources" ${this._sectionState.get(`${id}:sources`) ? "open" : ""}><summary>Automatic labels from other entities</summary>
      <p class="muted">Use camera human detection, any on/off or true/false entity, or Bermuda's current Area sensor. Choose independent sources, not this radar's own presence output. Any positive source starts or continues a presence period.</p>
      <div class="source-rows">${(config.sources || []).map((source, index) => this._sourceRowHtml(id, source, index)).join("")}</div>
      <button data-action="source-add" type="button" ${(config.sources || []).length >= 16 ? "disabled" : ""}>Add source</button>
      <div class="storage-fields"><label class="source-negative"><input type="checkbox" data-source-negative ${config.mark_not_present ? "checked" : ""}> Mark “Not Present”</label>
      <label>Source confidence (%)<input type="number" data-source-confidence min="1" max="100" value="${this._esc(config.confidence ?? 90)}" required></label></div>
      <div class="storage-fields"><label>Trim start of presence (seconds)<input type="number" data-source-buffer="start_buffer_seconds" min="0" max="3600" step="any" value="${this._esc(config.start_buffer_seconds ?? 10)}" required></label>
      <label>Trim end of presence (seconds)<input type="number" data-source-buffer="end_buffer_seconds" min="0" max="3600" step="any" value="${this._esc(config.end_buffer_seconds ?? 10)}" required></label></div>
      <p class="muted">Exclude these seconds from the start and end of each continuous external-presence period. If nothing remains, the period contributes no automatic presence labels. Raw readings and manual labels are kept. End trimming holds back automatic labels until enough time has passed; it does not delay the radar’s presence output. Set both to 0 for immediate labels. Changes affect future periods.</p>
      <p class="muted">Not Present is off by default. If enabled, every selected source must report absence; unavailable sources never count as absence. Use only when your sources cover everyone in the room. Confidence is your chosen training weight, not measured accuracy. Confirmed labels also teach future radar guesses. Manual labels win. Changing these sources resets their room-reference contributions. Recording must be enabled. Below ${d.presence_sources?.learning_minimum_confidence ?? 55}% confidence, readings are recorded but excluded from learning.</p>
      <p class="muted">${this._esc(status)}${d.recording_enabled === false ? " Recording is paused." : ""}</p>
      ${(d.presence_sources?.readings || []).map((source) => `<div class="muted">${this._esc(source.entity_id)}: ${source.present == null ? "No usable state" : source.present ? "Present" : "Off / outside target area"}</div>`).join("")}
      <button data-action="sources-save" type="button">Save presence sources</button></details>`;
  },

  _sourceRowHtml(id, source, index) {
    const listId = `source-options-${id}-${index}`;
    const options = Object.entries(this._hass?.states || {}).filter(
      ([entityId, state]) =>
        source.kind === "bermuda"
          ? entityId.startsWith("sensor.") &&
            "area_id" in (state.attributes || {})
          : ["on", "off", "true", "false"].includes(state.state) ||
            /^(binary_sensor|input_boolean|switch)\./.test(entityId),
    );
    return `<fieldset class="source-row" data-source-row><legend>Source ${index + 1}</legend><div class="storage-fields">
      <label>Type<select data-source-kind><option value="boolean" ${source.kind !== "bermuda" ? "selected" : ""}>Boolean entity</option><option value="bermuda" ${source.kind === "bermuda" ? "selected" : ""}>Bermuda area</option></select></label>
      <label>Entity<input data-source-entity list="${this._esc(listId)}" value="${this._esc(source.entity_id || "")}" required placeholder="binary_sensor.camera_person"><datalist id="${this._esc(listId)}">${options.map(([entityId, state]) => `<option value="${this._esc(entityId)}">${this._esc(state.attributes?.friendly_name || entityId)}</option>`).join("")}</datalist></label>
      ${source.kind === "bermuda" ? `<label>Target area ID or name<input data-source-area value="${this._esc(source.area || "")}" required placeholder="bedroom"></label>` : ""}</div>
      <button type="button" data-source-remove="${index}">Remove source</button></fieldset>`;
  },

  _readSources(form) {
    return {
      sources: [...form.querySelectorAll("[data-source-row]")].map((row) => ({
        kind: row.querySelector("[data-source-kind]").value,
        entity_id: row.querySelector("[data-source-entity]").value.trim(),
        area: row.querySelector("[data-source-area]")?.value.trim() || "",
      })),
      mark_not_present: form.querySelector("[data-source-negative]").checked,
      confidence: Number(form.querySelector("[data-source-confidence]").value),
      ...Object.fromEntries(
        [...form.querySelectorAll("[data-source-buffer]")].map((input) => [
          input.dataset.sourceBuffer,
          Number(input.value),
        ]),
      ),
    };
  },

  _wireSources(card, id, d) {
    const form = card.querySelector(".presence-sources");
    this._sourceDrafts ||= new Map();
    const capture = () => this._sourceDrafts.set(id, this._readSources(form));
    const redraw = () => {
      form.outerHTML = this._sourcesHtml(id, d);
      this._wireSources(card, id, d);
    };
    form.ontoggle = () => this._sectionState.set(`${id}:sources`, form.open);
    form.oninput = capture;
    form.onchange = (event) => {
      capture();
      if (event.target.matches("[data-source-kind]")) redraw();
    };
    form.querySelector('[data-action="source-add"]').onclick = () => {
      capture();
      this._sourceDrafts
        .get(id)
        .sources.push({ kind: "boolean", entity_id: "", area: "" });
      redraw();
    };
    for (const button of form.querySelectorAll("[data-source-remove]"))
      button.onclick = () => {
        capture();
        this._sourceDrafts
          .get(id)
          .sources.splice(Number(button.dataset.sourceRemove), 1);
        redraw();
      };
    const save = form.querySelector('[data-action="sources-save"]');
    save.onclick = () =>
      this._action(save, async () => {
        if (
          ![...form.querySelectorAll("input")].every((input) =>
            input.reportValidity(),
          )
        )
          return;
        await this._call("configure_presence_sources", {
          device_id: id,
          settings: this._readSources(form),
        });
        this._sourceDrafts.delete(id);
        await this._load(true);
      });
  },
};
