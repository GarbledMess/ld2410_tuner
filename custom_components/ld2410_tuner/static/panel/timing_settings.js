import { readNumberFields } from "./forms.js";

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
    this._updateHtml(
      container,
      `<details ${this._timingOpen ? "open" : ""}><summary>Learning timing</summary>
      <div class="storage-fields"><label>Timing model<select data-action="timing-mode">${TIMING_MODES.map(([value, label]) => `<option value="${value}" ${values.mode === value ? "selected" : ""}>${label}</option>`).join("")}</select></label></div>
      <p>Applies to manual and overnight learning on every device. Device values take priority; defaults fill only missing or unreadable values and are reported as assumptions.</p>
      <div class="storage-fields">${TIMING_FIELDS.map(([key, label, fallback]) => `<label>${label}<input type="number" data-timing="${key}" min="0" max="65535" step="any" required value="${this._esc(values[key] ?? fallback)}" ${values.mode === "fallback" ? "" : "disabled"}></label>`).join("")}</div>
      <p class="muted">Disabling timing scores raw threshold activity without radar timeout or on/off delays. These settings change the learning model only; they never change the device. Saved results keep their original timing: learn again after changing this policy.</p>
      <button data-action="timing-save">Save timing settings</button>
      <div class="settings-action-status muted" role="status"></div>
    </details>`,
    );
    container.querySelector("details").ontoggle = (event) => {
      this._timingOpen = event.target.open;
    };
    const mode = container.querySelector('[data-action="timing-mode"]');
    const inputs = [...container.querySelectorAll("[data-timing]")];
    const capture = () => ({
      mode: mode.value,
      ...readNumberFields(inputs, "timing"),
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
  _timingNeedsReview(learning) {
    const timing = learning.timing;
    if (timing?.configuration?.mode === "disabled") return false;
    return (
      timing?.scope !== "reported_presence" ||
      Object.values(timing?.configuration?.sources || {}).includes("fallback")
    );
  },

  _timingChanged(learning, current) {
    const captured = learning?.timing?.configuration;
    return Boolean(
      captured &&
      current &&
      ((captured.mode || "device") !== (current.mode || "device") ||
        ["timeout", "on_delay", "off_delay"].some(
          (key) =>
            (captured[key] ?? null) !== (current[key] ?? null) ||
            (captured.sources &&
              (captured.sources[key] || null) !==
                (current.sources?.[key] || null)),
        )),
    );
  },

  _timingHtml(learning, current) {
    const timing = learning?.timing;
    if (learning && !timing)
      return '<div class="notice timing-report">This saved result has no timing assessment. Learn again to use the current model and device settings.</div>';
    const settings = timing?.configuration || current || {};
    const duration = (value) =>
      value == null ? "unknown" : `${Number(value)}s`;
    const source = timing
      ? "Timing used for this result"
      : "Available device timing";
    const values = `Radar timeout: ${duration(settings.timeout)} · On delay: ${duration(settings.on_delay)} · Off delay: ${duration(settings.off_delay)}`;
    const scope = this._timingScopeText(settings);
    const uncertainty = learning?.training?.onset_uncertainty;
    const uncertaintyHtml = uncertainty?.samples
      ? `<p><b>${Number(uncertainty.samples)} presence observations have unresolved transition timing.</b> The signal recovered between snapshots, so the actual transition and any on delay may precede the high snapshot. The missed-presence range shows both possible onset times. These observations are retained. Their conservative missed-time estimate contributes to the finite error cost; uncertainty does not impose an absolute threshold requirement.</p>`
      : "";
    const changed = this._timingChanged(learning, current)
      ? "<p><b>Device timing has changed or the global timing policy was updated. Learn again to assess the current settings; the saved result still uses the values shown here.</b></p>"
      : "";
    return `<div class="notice timing-report"><b>${source}</b><div>${values}</div><p>${scope}</p>${this._paragraphHtml(this._timingPolicyNote(timing))}${changed}${learning?.status === "uncertain" ? "<p>Sampling uncertainty: recordings do not establish every transition time, and very short periods may have no measurable duration. This can remain even when all three device timing settings are known. The estimates and sample counts below show what was measured.</p>" : ""}${uncertaintyHtml}<details data-detail="timing-assumptions"><summary>Sampling and timing assumptions</summary>
      ${this._timingAssumptionsHtml(learning)}</details></div>`;
  },

  _timingScopeText(settings) {
    if (settings.mode === "disabled")
      return "Timing adjustments are disabled. Scores use raw threshold activity without timeout or on/off delays. Apply still changes gate thresholds only.";
    if (settings.timeout == null)
      return "Timeout is unavailable: time scores estimate raw threshold activity between snapshots. Your existing firmware remains supported.";
    if (settings.on_delay == null || settings.off_delay == null)
      return "Learning accounts for radar hold only. ESPHome filters are unknown; expose both delays or configure global fallback values to include them. The package is optional.";
    return "Learning accounts for radar hold, then delayed on and delayed off. These are read-only inputs; Apply changes gate thresholds only.";
  },

  _timingAssumptionsHtml(learning) {
    const timing = learning?.timing;
    const raw = learning?.raw_training;
    const interval = timing?.sample_interval_seconds;
    const paragraphs = [];
    if (timing?.active)
      paragraphs.push(
        `Sampled estimate: consecutive high readings are treated as one run; empty-room spikes allow for activity between observations. Gaps and label changes restart the replay. ${learning.training?.timing_warmup_samples || 0} initial observations were left unscored because the preceding device state is unknown.`,
      );
    if (interval != null)
      paragraphs.push(
        `Typical recorded interval: ${Number(interval)}s. These snapshots cannot establish sub-second spike lengths or exact detection times.`,
      );
    if (raw && timing?.active)
      paragraphs.push(
        `Before hold and filters: ${raw.false_negatives} missed / ${raw.present_samples} presence observations; ${raw.false_positives} crossings / ${raw.not_present_samples} empty observations. The quality measurements below use the timing estimate.`,
      );
    return paragraphs.map((text) => this._paragraphHtml(text)).join("");
  },
};
