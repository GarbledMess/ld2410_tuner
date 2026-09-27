const SLOT_LABELS = {
  user: "User learnt",
  previous: "Previous",
  current: "Current",
  automatic: "Autolearnt",
};

export const panelSavedResults = {
  _savedResults(d) {
    return (
      d.learning_results || (d.last_learning ? { user: d.last_learning } : {})
    );
  },

  _selectedLearning(id, d) {
    const saved = this._savedResults(d);
    const slot =
      this._learningSelection.get(id) ||
      ["user", "automatic", "current", "previous"].find((key) => saved[key]) ||
      "user";
    return { slot, result: saved[slot] || null };
  },

  _learningView(id, d) {
    if (!d) return d;
    const selected = this._selectedLearning(id, d);
    return {
      ...d,
      learning_slot: selected.slot,
      last_learning: selected.result,
    };
  },

  _savedResultsHtml(id, d) {
    const { slot, result } = this._selectedLearning(id, this._data.devices[id]);
    const saved = this._savedResults(this._data.devices[id]);
    const options = Object.entries(SLOT_LABELS)
      .map(([key, label]) => {
        const value = saved[key];
        const stamp = value?.created_at
          ? ` · ${new Date(value.created_at * 1000).toLocaleString()}`
          : "";
        const outcome = value
          ? this._applyOutcome(value, d.timing_configuration).label
          : "No result";
        return `<option value="${key}" ${key === slot ? "selected" : ""} ${value ? "" : "disabled"}>${this._esc(`${label} · ${outcome}${stamp}`)}</option>`;
      })
      .join("");
    const stale =
      result?.label_revision != null &&
      result.label_revision !== (d.history?.revision || 0);
    return `<label class="saved-result-picker">Saved thresholds<select data-action="learning-result">${options}</select></label>
      <div class="muted">User learnt: latest manual run. Autolearnt: latest overnight run. Current: last fully applied result. Previous: settings replaced by that Apply. Live device values are shown in the gate table.</div>
      ${stale ? '<div class="notice">Labels have changed since this result was learned. Its accuracy report describes the earlier labels; you can still apply these saved values.</div>' : ""}`;
  },

  _refreshRecoveryProgress() {
    for (const card of this.shadowRoot.querySelectorAll("[data-device-id]")) {
      const target = card.querySelector(".recovery-progress");
      const device = this._data?.devices?.[card.dataset.deviceId];
      if (target && device) target.innerHTML = this._recoveryHtml(device);
    }
  },

  _recoveryHtml(d) {
    const report = d.configuration_recovery;
    if (!report) return "";
    const stages = {
      query_parameters: "Reading radar parameters",
      radar_restart: "Restarting the radar",
      bluetooth_cycle: "Cycling Bluetooth and restoring its original state",
    };
    const outcomes = {
      running: stages[report.stage] || "Checking radar settings",
      recovered: "Radar settings recovered; learning can continue",
      failed: "Radar recovery failed",
      interrupted: "Radar recovery was interrupted",
    };
    return `<div class="notice recovery-report ${report.status === "running" ? "is-busy" : ""}" role="status"><b>${this._esc(outcomes[report.status] || report.status)}</b><p>${this._esc((report.steps || []).map((step) => stages[step] || step).join(" → "))}</p>${report.error ? `<p>${this._esc(report.error)}</p>` : ""}${report.restore_bluetooth ? "<p>Original Bluetooth state still needs restoration. The next Learn retries restoration first.</p>" : ""}</div>`;
  },

  _nightlyReportHtml(d) {
    const run = d.nightly_learning;
    if (!run) return "";
    const messages = {
      running: "Learning is running. You can keep using the panel.",
      ok: "Completed. Select Autolearnt to review the saved result’s accuracy, timing and learning model.",
      tradeoff:
        "Completed and scored: the presence-time target is met, with some false-active time. Select Autolearnt to review the score and the periods driving it.",
      unsafe:
        "Completed with presence gaps or continuous false activity in its recorded assessment. Select Autolearnt to review it.",
      uncertain:
        "Completed, but timing between recordings or very short sessions remains uncertain. Select Autolearnt to review the uncertainty; presence accuracy is not confirmed.",
      insufficient:
        "Completed, but there are too few usable examples. Record both occupied and empty periods, then learn again.",
      interrupted:
        "Learning stopped when the integration restarted. Learn again, or wait for the next scheduled run.",
      error: this._nightlyErrorText(run.error),
    };
    const date = new Date(
      (run.finished_at || run.started_at) * 1000,
    ).toLocaleString();
    return `<div class="notice nightly-report" role="status"><b>Last overnight run · ${this._esc(date)}</b>
      <p>${this._esc(messages[run.status] || run.status)}</p>
      ${run.attempts > 1 ? "<p>Retried once using the updated labels.</p>" : ""}
      ${run.error ? `<details data-detail="nightly-reason"><summary>Technical reason</summary><p>${this._esc(run.error)}</p></details>` : ""}</div>`;
  },

  _nightlyErrorText(error = "") {
    if (error.includes("Maximum distance gate"))
      return "Could not read valid maximum-distance settings from the radar. Check that its configuration entities show values in Home Assistant, then learn again.";
    if (error.includes("Training labels changed"))
      return "Presence labels changed during calculation, so this result was discarded. Finish editing the labels, then learn again.";
    if (
      error.includes("configuration changed") ||
      error.includes("Device timing changed") ||
      error.includes("configuration is unavailable")
    )
      return "Radar settings changed or became unavailable during learning. Check the device settings, then learn again.";
    return "Learning could not finish. Check the technical reason below, then retry with Learn thresholds.";
  },

  _nightlyMarker(d) {
    const run = d.nightly_learning;
    if (!run)
      return '<span class="pill nightly-marker">Overnight: not run</span>';
    const labels = {
      running: "Learning…",
      ok: "Completed · review result",
      tradeoff: "Scored · review penalty",
      unsafe: "Targets not met",
      insufficient: "Not enough data",
      uncertain: "Timing uncertain",
      error: "Failed",
      interrupted: "Interrupted",
    };
    const style =
      run.status === "running"
        ? "is-busy"
        : run.status === "ok"
          ? "ok"
          : "warn";
    const date = new Date(
      (run.finished_at || run.started_at) * 1000,
    ).toLocaleString();
    return `<button type="button" data-action="review-nightly" class="pill nightly-marker ${style}" aria-label="Review last overnight run" title="${this._esc(`${date}${run.error ? ` · ${run.error}` : ""}`)}">Overnight: ${this._esc(labels[run.status] || run.status)}</button>`;
  },

  _wireSavedResults(card, id) {
    const marker = card.querySelector('[data-action="review-nightly"]');
    if (marker)
      marker.onclick = (event) => {
        event.stopPropagation();
        this._openRecommendations(card, id);
      };
    const select = card.querySelector('[data-action="learning-result"]');
    select.onchange = () => {
      this._learningSelection.set(id, select.value);
      select.blur();
      this._draw();
      this._renderChartCanvas(id, true);
    };
  },

  _drawLearningSchedule() {
    const container = this.shadowRoot.querySelector("#learning-schedule");
    const config = this._data?.learning_schedule || {
      enabled: false,
      time: "03:00",
      timezone: "Home Assistant timezone",
    };
    const form = this._scheduleDraft || config;
    container.innerHTML = `<label><input data-action="nightly-enabled" type="checkbox" ${form.enabled ? "checked" : ""}> Overnight learning</label>
      <label>Time <input data-action="nightly-time" type="time" value="${this._esc(form.time)}" required></label>
      <button data-action="nightly-save">Save schedule</button>
      <span class="muted ${config.running ? "is-busy" : ""}" role="status">${this._esc(config.timezone)} · ${config.running ? "Learning devices…" : "Saves results only; Apply stays manual."}</span>`;
    container.oninput = () => {
      this._scheduleDraft = {
        enabled: container.querySelector('[data-action="nightly-enabled"]')
          .checked,
        time: container.querySelector('[data-action="nightly-time"]').value,
      };
    };
    const save = container.querySelector('[data-action="nightly-save"]');
    save.onclick = () =>
      this._action(save, async () => {
        const at = container.querySelector('[data-action="nightly-time"]');
        if (!at.reportValidity()) return;
        await this._call("configure_learning_schedule", {
          enabled: container.querySelector('[data-action="nightly-enabled"]')
            .checked,
          at: at.value,
        });
        this._scheduleDraft = null;
        await this._load(true);
      });
  },
};
