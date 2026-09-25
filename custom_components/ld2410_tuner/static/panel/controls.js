export const panelControls = {
  _wireCard(card, id, d) {
    this._wireCollapse(card, id, d);
    this._wireTraining(card, id, d);
    this._wireHistory(card, id);
    this._wireActions(card, id);
    this._wireChartControls(card, id);
    this._restoreDraft(card, id);
    this._wireHistoryRange(card, id);
  },
  _wireCollapse(card, id, d) {
    const toggleBody = () => {
      const willCollapse = !this._collapsed.has(id);
      if (willCollapse) this._collapsed.add(id);
      else this._collapsed.delete(id);
      const body = card.querySelector(".body");
      const btn = card.querySelector('[data-action="toggle"]');
      body.classList.toggle("collapsed", willCollapse);
      btn.textContent = willCollapse ? "▸" : "▾";
      btn.setAttribute("aria-expanded", String(!willCollapse));
      btn.setAttribute("aria-label", willCollapse ? "Expand" : "Collapse");
      if (!willCollapse) this._maybeFetchChart(id, d);
    };
    card.querySelector('[data-action="toggle"]').onclick = (e) => {
      e.stopPropagation();
      toggleBody();
    };
    card.querySelector('[data-action="toggle-top"]').onclick = toggleBody;

    card.querySelectorAll('[data-action="section-toggle"]').forEach((head) => {
      head.onclick = () => {
        const key = head.dataset.section;
        const willCollapse = !this._sectionCollapsed(id, key);
        this._setSectionCollapsed(id, key, willCollapse);
        const sub = head.closest(".subsection");
        sub.classList.toggle("collapsed", willCollapse);
        const btn = head.querySelector(".sub-toggle");
        btn.textContent = willCollapse ? "▸" : "▾";
        btn.setAttribute("aria-expanded", String(!willCollapse));
        if (key === "chart" && !willCollapse) this._maybeFetchChart(id, d);
      };
    });
  },
  _wireTraining(card, id, d) {
    const timeoutHours = card.querySelector('[data-action="timeout-hours"]');
    const timeoutMinutes = card.querySelector(
      '[data-action="timeout-minutes"]',
    );
    const timeoutTotal = Number(d.training_timeout_seconds || 0);
    timeoutHours.value = String(Math.floor(timeoutTotal / 3600));
    timeoutMinutes.value = String(Math.floor((timeoutTotal % 3600) / 60));
    const saveTimeout = async () => {
      const current = card.querySelector('[data-action="state"]').value;
      let hours = Math.max(
        0,
        Number.parseInt(timeoutHours.value || "0", 10) || 0,
      );
      let minutes = Math.max(
        0,
        Math.min(59, Number.parseInt(timeoutMinutes.value || "0", 10) || 0),
      );
      timeoutHours.value = String(hours);
      timeoutMinutes.value = String(minutes);
      if (current !== "unknown")
        await this._call("set_training_state", {
          device_id: id,
          state: current,
          timeout_seconds: hours * 3600 + minutes * 60,
        });
      else if (hours || minutes)
        await this._call("set_training_state", {
          device_id: id,
          state: current,
          timeout_seconds: 0,
        });
      await this._load(true);
    };
    card.querySelector('[data-action="state"]').onchange = (e) =>
      this._action(e.currentTarget, async () => {
        await this._call("set_training_state", {
          device_id: id,
          state: e.target.value,
          timeout_seconds:
            e.target.value === "unknown"
              ? 0
              : Number(timeoutHours.value || 0) * 3600 +
                Number(timeoutMinutes.value || 0) * 60,
        });
        await this._load(true);
      });
    timeoutHours.onchange = (e) => this._action(e.currentTarget, saveTimeout);
    timeoutMinutes.onchange = (e) => this._action(e.currentTarget, saveTimeout);
  },
  async _learnThresholds(id) {
    await this._call("learn", { device_id: id }, 120000);
    this._setSectionCollapsed(id, "details", false);
    await this._load(true);
  },

  async _applyRecommendation(id) {
    if (
      !confirm(
        "Apply these recommended thresholds? Inferred results are estimates; verify quiet presence and empty-room behaviour.",
      )
    )
      return;
    const result = await this._call("apply", { device_id: id }, 240000);
    await this._load(true);
    if (Object.keys(result.skipped || {}).length)
      throw new Error(
        `Reported matches for ${Object.keys(result.applied || {}).length} thresholds. Incomplete: ${Object.entries(
          result.skipped,
        )
          .map(([key, reason]) => `${key}: ${reason}`)
          .join("; ")}`,
      );
    if (result.note) alert(result.note);
  },

  async _clearDevice(id) {
    if (
      !confirm(
        "Clear training, history and learned thresholds for this device?",
      )
    )
      return;
    await this._call("clear", { device_id: id });
    this._chartState.delete(id);
    this._historyState.delete(id);
    this._drafts.delete(id);
    await this._load(true);
  },

  _wireActions(card, id) {
    const actions = {
      learn: () => this._learnThresholds(id),
      apply: () => this._applyRecommendation(id),
      clear: () => this._clearDevice(id),
      json: () => this._export(id, "json"),
      csv: () => this._export(id, "csv"),
    };
    for (const [name, action] of Object.entries(actions))
      card.querySelector(`[data-action="${name}"]`).onclick = (event) =>
        this._action(event.currentTarget, action);
    card.querySelectorAll('[data-action="auto-feedback"]').forEach((button) => {
      button.onclick = () =>
        this._action(button, async () => {
          await this._call("auto_feedback", {
            device_id: id,
            correct: button.dataset.correct === "true",
          });
          await this._load(true);
        });
    });
  },
  _wireChartControls(card, id) {
    const chartKind = card.querySelector('[data-action="chart-kind"]');
    const chartRange = card.querySelector('[data-action="chart-range"]');
    const chartRefresh = card.querySelector('[data-action="chart-refresh"]');
    const cs = this._chartState.get(id);
    chartKind.onchange = () => {
      cs.kind = chartKind.value;
      this._fetchChartData(id);
    };
    chartRange.onchange = () => {
      cs.hours = Number(chartRange.value);
      cs.selection = null;
      cs.panelOpen = false;
      this._fetchChartData(id);
    };
    chartRefresh.onclick = () => {
      this._fetchChartData(id, true);
    };
    card.querySelectorAll('[data-action="chart-pick-gate"]').forEach((btn) => {
      btn.onclick = () => {
        cs.gate = Number(btn.dataset.gate);
        this._updateGateChipHighlight(card, cs.gate);
        this._renderChartCanvas(id);
      };
    });
  },
  _restoreDraft(card, id) {
    const draft = this._drafts.get(id);
    if (!draft) return;
    for (const [action, value] of Object.entries(draft)) {
      const field = card.querySelector(`[data-action="${action}"]`);
      if (field) field.value = value;
    }
  },
};
