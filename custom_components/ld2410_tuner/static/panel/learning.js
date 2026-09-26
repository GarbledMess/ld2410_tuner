export const panelLearning = {
  _learningWarningHtml(learning) {
    if (!learning?.warnings?.length) return "";
    const style = learning.status === "unsafe" ? "warn" : "";
    return `<div class="notice ${style}">${learning.warnings.map(this._esc).join("<br>")}</div>`;
  },

  _presenceDependencyText(proposal) {
    const count = proposal.exclusive_presence_samples;
    if (count == null) return "";
    if (!count)
      return " No labelled presence sample depends on this gate alone.";
    return ` ${count} labelled presence samples depend on this gate alone; weakest energy ${proposal.weakest_exclusive_presence_energy}.`;
  },

  _outlierFilterHtml(learning) {
    const filtered = learning?.outlier_filter;
    const human = filtered?.human?.excluded?.present || 0;
    const automatic = filtered?.automatic?.excluded?.present || 0;
    if (!human && !automatic) return "";
    const periods = (filtered.human?.periods || []).map((period) => {
      const from = new Date(period.start * 1000).toLocaleString();
      const to = new Date(period.end * 1000).toLocaleTimeString();
      return `${from} – ${to} (${period.samples} samples)`;
    });
    return `<div class="notice outlier-filter"><b>Excluded outliers: ${human} human-labelled / ${automatic} estimated presence samples.</b>
      <div>Rare, separated low readings in brief dips are excluded before learning. The same retained observations determine thresholds, accuracy, episodes, feasibility and recommendation quality.</div>
      <div>Human-labelled exclusions: ${this._esc(periods.join("; ") || "none")}.</div>
      <div>Original recordings and labels remain available. Sustained quiet presence, readings supported by another gate, and empty-room noise spikes are retained.</div></div>`;
  },

  _feasibilityHtml(learning) {
    if (learning?.feasibility?.status !== "conflict") return "";
    const window = learning.feasibility.windows?.find((item) => item.conflict);
    if (!window) return "";
    const scope =
      window.scope === "recent_human" ? "Recent human labels" : "Human labels";
    const minimum = window.minimum_false_samples_for_recall;
    const explanation =
      minimum == null
        ? "Some labelled presence readings cannot trigger any gate at a valid threshold."
        : `Meeting the presence recall target requires at least ${minimum} / ${window.not_present_samples} false-trigger samples; the limit is ${window.allowed_false_samples}.`;
    const periods = (window.periods || []).map((period) => {
      const from = new Date(period.start * 1000).toLocaleString();
      const to = new Date(period.end * 1000).toLocaleTimeString();
      return `<button type="button" data-action="review-period" data-start="${Number(period.start)}" data-end="${Number(period.end)}">${this._esc(`${from} – ${to} (${period.samples} samples)`)}</button>`;
    });
    return `<div class="notice evidence-conflict"><b>${scope}: the retained observations cannot meet both targets with any gate-threshold combination.</b>
      <div>${this._esc(explanation)}</div>
      <div>Within the false-trigger budget, at least ${window.unavoidable_missed_samples} presence samples are missed (allowed ${window.allowed_missed_samples}); the longest unavoidable run is ${window.unavoidable_longest_missed_run} samples.</div>
      <div>Recorded conflicts around: ${periods.join(" ")}.</div>
      <div>Review these periods in Past presence labels. This does not establish that the labels are wrong; the recorded signals may overlap. Labels and thresholds have not been changed.</div></div>`;
  },

  _confidenceText(value) {
    return value == null ? "—" : Math.round(value * 100) + "%";
  },

  _inferenceHtml(inferred) {
    if (!inferred) return "";
    const presentConfidence = this._confidenceText(
      inferred.mean_confidence?.present,
    );
    const absentConfidence = this._confidenceText(
      inferred.mean_confidence?.not_present,
    );
    const deferred = inferred.deferred_samples
      ? ` ${inferred.deferred_samples} newer estimates await later human-labelled validation.`
      : "";
    return `<div class="notice">Automatic evidence: ${inferred.samples?.present || 0} present / ${inferred.samples?.not_present || 0} empty estimates. Mean confidence: ${presentConfidence} / ${absentConfidence}. Weighted automatic observations: ${(inferred.effective_weight?.present || 0).toFixed(1)} / ${(inferred.effective_weight?.not_present || 0).toFixed(1)}. Human-labelled results always take priority.${deferred}</div>`;
  },

  _humanValidationHtml(learning) {
    const validation = learning?.training;
    if (
      !validation ||
      !(validation.present_samples || validation.not_present_samples)
    ) {
      if (!Object.keys(learning?.proposals || {}).length) return "";
      return '<div class="notice">No human-labelled measurement is available. This recommendation uses confidence-weighted estimates; test it in the room.</div>';
    }
    const sensitivity = validation.present_samples
      ? (validation.sensitivity * 100).toFixed(2) + "%"
      : "not measured";
    const falseRate = validation.not_present_samples
      ? (validation.false_positive_rate * 100).toFixed(2) + "% of empty samples"
      : "no human-labelled empty samples";
    const recommendation =
      learning.status === "ok"
        ? "Recommendation available."
        : "The combined thresholds did not meet the targets.";
    return `<div class="notice">Retained human-labelled observations: <b>${validation.false_negatives ?? "—"} missed / ${validation.present_samples} presence samples</b> (${sensitivity}; target 99.9%). Missed episodes: ${validation.missed_presence_episodes ?? "—"}; longest missed run: ${validation.longest_missed_run_samples ?? "—"} samples. False-trigger bursts: ${validation.false_trigger_bursts ?? "—"} (${falseRate}). ${recommendation}</div>`;
  },

  _validationHtml(learning) {
    const backtest = learning?.validation;
    const backtestHtml = backtest
      ? `<div class="notice">Earlier-data backtest: ${backtest.false_negatives ?? 0} missed presence samples; ${backtest.false_positives ?? 0} false triggers. The final recommendation uses all retained observations.</div>`
      : "";
    return (
      this._humanValidationHtml(learning) +
      this._outlierFilterHtml(learning) +
      this._feasibilityHtml(learning) +
      backtestHtml +
      this._falsePositiveSourcesHtml(learning) +
      this._inferenceHtml(learning?.automatic_evidence)
    );
  },

  _applyOutcome(learning) {
    if (!Object.keys(learning?.proposals || {}).length)
      return { level: "none", label: "No learned results", enabled: false };
    if (
      learning.status === "unsafe" ||
      learning.feasibility?.status === "conflict"
    )
      return { level: "bad", label: "Targets not met", enabled: true };
    const measured = (metrics) =>
      metrics?.present_samples > 0 && metrics?.not_present_samples > 0;
    if (
      learning.status !== "ok" ||
      learning.method !== "human_priority_v6" ||
      !measured(learning.training) ||
      !measured(learning.validation) ||
      this._backtestHasIssues(learning)
    )
      return { level: "caution", label: "Review concerns", enabled: true };
    return { level: "good", label: "Targets met", enabled: true };
  },

  _backtestHasIssues(learning) {
    const metrics = learning.validation;
    const targets = learning.targets || {};
    if (metrics.sensitivity < (targets.sensitivity ?? 0.999)) return true;
    return Object.entries({
      false_positive_rate: 0.005,
      missed_presence_episodes: 0,
      longest_missed_run_samples: 1,
      false_trigger_bursts_per_hour: 1,
    }).some(([key, fallback]) => metrics[key] > (targets[key] ?? fallback));
  },

  _recommendationsHtml(d, id) {
    const learning = d.last_learning;
    const outcome = this._applyOutcome(learning);
    return `${this._savedResultsHtml(id, d)}<div class="controls"><button class="primary" data-action="learn">Learn thresholds</button><button class="apply-${outcome.level}" data-action="apply" ${outcome.enabled ? "" : "disabled"}>Apply learned thresholds · ${outcome.label}</button></div>
      <div class="muted">Learn creates a preview. Apply writes it to the device even when quality targets are missed. Red: targets not met; yellow: limited evidence or review concerns; green: measured targets met. Presence requires any enabled gate to trigger; any gate triggering in an empty room is a device false positive. A threshold of 100 disables that gate.</div>
      ${this._learningWarningHtml(learning)}${this._validationHtml(learning)}
      <details class="gate-results"><summary>Gate thresholds and sample counts</summary>${this._detailsHtml(d)}</details>`;
  },

  _dataActionsHtml() {
    return `<div class="export"><button data-action="json">Export JSON</button><button data-action="csv">Export CSV</button></div>
      <div class="data-clear"><span class="muted">Remove recorded history, labels and recommendations for this device.</span><button data-action="clear">Clear data</button></div>`;
  },
};
