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
        const outcome = value ? this._applyOutcome(value).label : "No result";
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

  _nightlyMarker(d) {
    const run = d.nightly_learning;
    if (!run)
      return '<span class="pill nightly-marker">Overnight: not run</span>';
    const labels = {
      running: "Learning…",
      ok: "Targets met",
      unsafe: "Targets not met",
      insufficient: "Not enough data",
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
    return `<span class="pill nightly-marker ${style}" title="${this._esc(`${date}${run.error ? ` · ${run.error}` : ""}`)}">Overnight: ${this._esc(labels[run.status] || run.status)}</span>`;
  },

  _wireSavedResults(card, id) {
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
