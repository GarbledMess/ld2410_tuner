import { readNumberFields } from "./forms.js";
import { sourcePickerHtml, wireSourcePicker } from "./source_picker.js";

function areaNameKey(value) {
  // Expand case variants (including sharp S), while keeping dotless i distinct.
  return value
    .replaceAll("ẞ", "ss")
    .split("ı")
    .map((part) => part.toUpperCase().toLowerCase())
    .join("ı");
}

export const panelSources = {
  // Sources belong to rooms and zones. A radar keeps its own form only outside every
  // group, or while it still has (or is editing) sources of its own to move.
  _inAnyGroup(id) {
    return Object.values(this._roomsData().groups).some((group) =>
      group.device_ids?.includes(id),
    );
  },

  _radarSourcesShown(id, d) {
    return (
      !this._inAnyGroup(id) ||
      !!d.presence_sources?.sources?.length ||
      Object.values(this._data?.bermuda?.devices || {}).some(
        (device) => id in device.radars,
      ) ||
      !!this._sourceDrafts?.has(id)
    );
  },

  // ``groupId`` attaches the sources to a room group instead of a radar.
  _sourcesHtml(id, d, groupId = null) {
    const config = this._sourceDrafts?.get(id) || d.presence_sources || {};
    const status = this._sourceStatusText(d.presence_sources);
    const open = this._sectionState.get(`${id}:sources`) ? "open" : "";
    const full = (config.sources || []).length >= 16;
    const limitId = `source-limit-${Array.from(groupId || id, (char) => char.codePointAt(0).toString(16)).join("-")}`;
    const intro = groupId
      ? "Use camera human detection or any on/off or true/false entity to label this whole room. Configure tracked phones and watches in the Bermuda section. Their output is stored as read-only sensor room labels; your manual room labels always win."
      : "Use camera human detection or any on/off or true/false entity. Configure tracked phones and watches in the Bermuda section below the radar cards. Choose independent sources, not this radar's own presence output.";
    return `<details class="presence-sources" ${open}><summary>${groupId ? "Room presence sources" : this._inAnyGroup(id) ? "Radar presence sources (move these to its room)" : "Automatic labels from other entities"}<span class="section-description">Create presence labels from cameras and other sensors.</span></summary>
      <p class="muted">${intro} Any positive source starts or continues a presence period.</p>
      <div class="source-rows">${(config.sources || []).map((source, index) => this._sourceRowHtml(id, source, index, !!groupId)).join("")}</div>
      <button data-action="source-add" type="button" ${full ? `disabled aria-describedby="${limitId}"` : ""}>Add source</button>
      ${full ? `<p class="field-note" id="${limitId}">Up to 16 sources can be used. Remove one to add another.</p>` : ""}
      <div class="storage-fields"><label class="source-negative"><input type="checkbox" data-source-negative ${config.mark_not_present ? "checked" : ""}> Mark “Not Present”</label>
      <label>Source confidence (%)<input type="number" data-source-confidence min="1" max="100" value="${this._esc(config.confidence ?? 90)}" required></label></div>
      <div class="storage-fields"><label>Trim start of presence (seconds)<input type="number" data-source-buffer="start_buffer_seconds" min="0" max="3600" step="any" value="${this._esc(config.start_buffer_seconds ?? 10)}" required></label>
      <label>Trim end of presence (seconds)<input type="number" data-source-buffer="end_buffer_seconds" min="0" max="3600" step="any" value="${this._esc(config.end_buffer_seconds ?? 10)}" required></label></div>
      <p class="muted">Trim removes seconds from each end of a source's presence. With Mark "Not Present", every source reporting absence labels the room empty.</p>
      <p class="muted">${this._esc(status)}${d.recording_enabled === false ? " Recording is paused." : ""}</p>
      ${(d.presence_sources?.readings || []).map((source) => this._sourceReadingHtml(source)).join("")}
      <button data-action="sources-save" type="button">Save presence sources</button></details>`;
  },

  _sourceStatusText(sources) {
    if (sources?.buffering)
      return "Buffering presence: waiting for a usable interval after both trims.";
    const current = sources?.state || "unknown";
    if (current === "unknown")
      return "No external label; radar guessing continues.";
    const label = current === "present" ? "Present" : "Not Present";
    return `External label: ${label}.`;
  },

  _sourceReadingHtml(source) {
    let label = "No usable state";
    if (source.present != null)
      label = source.present ? "Present" : "Off / outside target area";
    // A distance comes only with a reading in the target area, where it decides.
    if ("distance" in source)
      label =
        source.distance == null
          ? "No usable distance"
          : `${source.present ? "Present" : "Too far"} (${source.distance} m of ${source.max_distance} m)`;
    return `<div class="muted">${this._esc(source.entity_id)}: ${this._esc(label)}</div>`;
  },

  _sourceAreaHtml(saved = "", placeholder = "Choose an area") {
    const areas = Object.entries(this._roomsData().areas).sort((a, b) =>
      a[1].localeCompare(b[1]),
    );
    const selected =
      areas.find(([id]) => id === saved)?.[0] ??
      areas.find(([, name]) => areaNameKey(name) === areaNameKey(saved))?.[0] ??
      saved;
    const options = [["", placeholder], ...areas];
    if (selected && !areas.some(([id]) => id === selected))
      options.push([selected, `${saved} (unknown area)`]);
    return `<label>Target area<select data-source-area required>${options.map(([id, name]) => `<option value="${this._esc(id)}" ${id === selected ? "selected" : ""}>${this._esc(name)}</option>`).join("")}</select></label>`;
  },

  _sourceRowHtml(id, source, index, room = false) {
    const listId = `source-options-${id}-${index}`;
    return `<fieldset class="source-row" data-source-row><legend>Source ${index + 1}</legend><div class="storage-fields">
      <label>Type<select data-source-kind><option value="boolean">Boolean entity</option></select></label>
      ${sourcePickerHtml((value) => this._esc(value), listId, source.entity_id)}</div>
      <button type="button" data-source-remove="${index}">Remove source</button></fieldset>`;
  },

  _readSources(form) {
    return {
      sources: [...form.querySelectorAll("[data-source-row]")].map((row) => ({
        kind: row.querySelector("[data-source-kind]").value,
        entity_id: row.querySelector("[data-source-entity]").value.trim(),
        area: row.querySelector("[data-source-area]")?.value || "",
        ...(row.querySelector("[data-source-max-distance]")?.value
          ? {
              max_distance: Number(
                row.querySelector("[data-source-max-distance]").value,
              ),
            }
          : {}),
      })),
      mark_not_present: form.querySelector("[data-source-negative]").checked,
      confidence: Number(form.querySelector("[data-source-confidence]").value),
      ...readNumberFields(
        form.querySelectorAll("[data-source-buffer]"),
        "sourceBuffer",
      ),
    };
  },

  _wireSources(card, id, d, groupId = null) {
    const form = card.querySelector(".presence-sources");
    if (!form) return; // This radar's sources live on its room (card.js).
    this._sourceDrafts ||= new Map();
    const capture = () => this._sourceDrafts.set(id, this._readSources(form));
    const redraw = () => {
      form.outerHTML = this._sourcesHtml(id, d, groupId);
      this._wireSources(card, id, d, groupId);
    };
    form.ontoggle = () => this._sectionState.set(`${id}:sources`, form.open);
    form.oninput = capture;
    for (const row of form.querySelectorAll("[data-source-row]"))
      wireSourcePicker(
        row,
        () => this._hass?.states,
        (value) => this._esc(value),
      );
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
    save.onclick = () => {
      if (
        ![...form.querySelectorAll("input, select")].every((input) =>
          input.reportValidity(),
        )
      )
        return;
      return this._action(save, async () => {
        const settings = this._readSources(form);
        await (groupId
          ? this._call("configure_room_sources", {
              group_id: groupId,
              settings,
            })
          : this._call("configure_presence_sources", {
              device_id: id,
              settings,
            }));
        this._sourceDrafts.delete(id);
        await this._load(true);
      });
    };
  },
};
