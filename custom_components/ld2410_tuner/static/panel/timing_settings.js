const TIMING_FIELDS = [
  ["timeout", "Fallback radar timeout (seconds)", 1],
  ["on_delay", "Fallback on delay (seconds)", 0.5],
  ["off_delay", "Fallback off delay (seconds)", 1],
];
const TIMING_MODES = [
  ["device", "Use available device timing"],
  ["fallback", "Use defaults where device timing is missing"],
  ["disabled", "Disable timing adjustments"],
];

export const panelTimingSettings = {
  _drawTimingSettings() {
    const container = this.shadowRoot.querySelector("#timing-controls");
    const values = this._timingDraft ||
      this._data?.timing_settings || { mode: "device" };
    container.innerHTML = `<details ${this._timingOpen ? "open" : ""}><summary>Learning timing</summary>
      <div class="storage-fields"><label>Timing model<select data-action="timing-mode">${TIMING_MODES.map(([value, label]) => `<option value="${value}" ${values.mode === value ? "selected" : ""}>${label}</option>`).join("")}</select></label></div>
      <p>Applies to manual and overnight learning on every device. Device values take priority; defaults fill only missing or unreadable values and are reported as assumptions.</p>
      <div class="storage-fields">${TIMING_FIELDS.map(([key, label, fallback]) => `<label>${label}<input type="number" data-timing="${key}" min="0" max="65535" step="any" required value="${this._esc(values[key] ?? fallback)}" ${values.mode === "fallback" ? "" : "disabled"}></label>`).join("")}</div>
      <p class="muted">Disabling timing scores raw threshold activity without radar timeout or on/off delays. These settings change the learning model only; they never change the device. Saved results keep their original timing: learn again after changing this policy.</p>
      <button data-action="timing-save">Save timing settings</button>
      <div class="settings-action-status muted" role="status"></div>
    </details>`;
    container.querySelector("details").ontoggle = (event) => {
      this._timingOpen = event.target.open;
    };
    const mode = container.querySelector('[data-action="timing-mode"]');
    const inputs = [...container.querySelectorAll("[data-timing]")];
    const capture = () => ({
      mode: mode.value,
      ...Object.fromEntries(
        inputs.map((input) => [input.dataset.timing, Number(input.value)]),
      ),
    });
    container.oninput = () => {
      this._timingDraft = capture();
    };
    mode.onchange = () => {
      this._timingDraft = capture();
      for (const input of inputs) input.disabled = mode.value !== "fallback";
    };
    const save = container.querySelector('[data-action="timing-save"]');
    save.onclick = () =>
      this._action(save, async () => {
        if (!inputs.every((input) => input.reportValidity())) return;
        await this._call("configure_timing", { settings: capture() });
        this._timingDraft = null;
        await this._load(true);
      });
  },

  _timingPolicyNote(timing) {
    const config = timing?.configuration;
    if (!config) return "";
    if (config.mode === "disabled")
      return "Timing adjustments disabled: this result scores raw threshold activity, not held or delayed presence.";
    const assumed = Object.entries(config.sources || {})
      .filter(([, source]) => source === "fallback")
      .map(([field]) => field.replaceAll("_", " "));
    if (assumed.length)
      return `Global defaults assumed for ${assumed.join(", ")}. These values were not read from the device.`;
    if (timing.scope !== "reported_presence")
      return "Timing metadata is incomplete. Learning completed using available timing; missing delays are not a job failure.";
    return "";
  },
};
