import { readNumberFields } from "./forms.js";

const STORAGE_FIELDS = [
  ["thin_after_days", "Start thinning after (days)", 0, 3649, 7],
  ["minimum_confidence", "Starting confidence cutoff (%)", 0, 100, 50],
  ["automatic_days", "Remove all automatic readings after (days)", 1, 3650, 30],
  ["human_days", "Remove human-labelled readings after (days)", 1, 3650, 30],
  ["max_mib", "Total file ceiling (MiB)", 1, 10240, 100],
];

export const panelStorage = {
  _recordingHtml(d) {
    const enabled = d.recording_enabled !== false;
    const blocked = this._data?.storage?.blocked;
    let status = enabled
      ? "Recording history and training evidence"
      : "Paused · existing data kept";
    if (blocked) status = "Paused · storage limit needs attention";
    return `<div class="recording-control"><label><input type="checkbox" role="switch" data-action="recording" ${enabled ? "checked" : ""}> Record data</label><span class="muted">${status}</span></div>`;
  },

  _wireRecording(card, id, d) {
    const control = card.querySelector('[data-action="recording"]');
    control.onchange = () =>
      this._action(control, async () => {
        try {
          await this._call("set_recording", {
            device_id: id,
            enabled: control.checked,
          });
        } catch (error) {
          control.checked = d.recording_enabled !== false;
          throw error;
        }
        await this._load(true);
      });
    if (d.recording_enabled === false)
      for (const action of ["state", "timeout-hours", "timeout-minutes"])
        card.querySelector(`[data-action="${action}"]`).disabled = true;
  },

  _storageStatusHtml() {
    const storage = this._data?.storage || {};
    const mib = (value) => (value / 1048576).toFixed(2);
    const size =
      storage.used_bytes == null
        ? "Size available after the next save."
        : `Last saved file: ${mib(storage.used_bytes)} MiB / ${storage.settings.max_mib} MiB ceiling.`;
    const last = storage.last_trim || storage;
    const trim = storage.last_trim
      ? ` Last cleanup removed ${last.removed_automatic} automatic/unlabelled and ${last.removed_human} human-labelled readings.`
      : "";
    const paused = storage.blocked
      ? "Recording paused to protect the storage limit. "
      : "";
    const error = storage.error
      ? `<div class="notice">${paused}${this._esc(storage.error)}</div>`
      : "";
    return `<span>${this._esc(size + trim)}</span>${error}`;
  },

  _drawStorage() {
    const container = this.shadowRoot.querySelector("#storage-controls");
    const values = this._storageDraft || this._data?.storage?.settings || {};
    this._updateHtml(
      container,
      `<details ${this._storageOpen ? "open" : ""}><summary>Recording storage${this._data?.storage?.error ? " · needs attention" : ""}</summary>
      <div class="storage-status" role="status">${this._storageStatusHtml()}</div>
      <p>After the thinning age, the confidence cutoff rises from the starting percentage to 100% at automatic expiry. Human labels take priority and expire at their own age. These settings cover every device.</p>
      <div class="storage-fields">${STORAGE_FIELDS.map(([key, label, min, max, fallback]) => `<label>${label}<input type="number" data-storage="${key}" min="${min}" max="${max}" step="any" required value="${this._esc(values[key] ?? fallback)}"></label>`).join("")}</div>
      <button data-action="storage-save">Save settings and clean now</button>
      <div class="storage-trim"><label>Trim to (MiB, optional)<input type="number" data-action="trim-target" min="1" step="any" value="${this._esc(this._trimTarget || "")}" placeholder="Current ceiling"></label><button data-action="storage-trim">Trim now</button></div>
      <p class="muted">The ceiling covers the tuner file across all devices. Size trimming removes automatic/unlabelled chunks first, lower confidence before higher confidence, then the oldest human-labelled chunks. Age and size trimming permanently remove history. Saved recommendations and settings are retained. Export important recordings before trimming.</p>
      <div class="storage-action-status muted" role="status"></div>
    </details>`,
    );
    container.querySelector("details").ontoggle = (event) => {
      this._storageOpen = event.target.open;
    };
    container.oninput = () => {
      this._storageDraft = readNumberFields(
        container.querySelectorAll("[data-storage]"),
        "storage",
      );
      this._trimTarget = container.querySelector(
        '[data-action="trim-target"]',
      ).value;
    };
    const save = container.querySelector('[data-action="storage-save"]');
    save.onclick = () =>
      this._action(save, async () => {
        const inputs = [...container.querySelectorAll("[data-storage]")];
        if (!inputs.every((input) => input.reportValidity())) return;
        if (
          !confirm(
            "Save these global settings and remove history outside their retention or size limits? This cannot be undone.",
          )
        )
          return;
        await this._call(
          "configure_storage",
          {
            settings: readNumberFields(inputs, "storage"),
          },
          120000,
        );
        this._storageDraft = null;
        await this._load(true);
      });
    const trim = container.querySelector('[data-action="storage-trim"]');
    trim.onclick = () =>
      this._action(trim, async () => {
        const input = container.querySelector('[data-action="trim-target"]');
        if (!input.reportValidity()) return;
        if (
          !confirm(
            "Trim recorded data now using the saved global policy and this size target? Removed history cannot be recovered.",
          )
        )
          return;
        await this._call(
          "trim_storage",
          input.value ? { target_mib: Number(input.value) } : {},
          120000,
        );
        await this._load(true);
      });
  },
};
