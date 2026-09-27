export const panelLearning = {
  _learningWarningHtml(learning) {
    if (!learning?.warnings?.length) return "";
    return `<ul class="learning-notes">${learning.warnings.map((note) => `<li>${this._esc(note)}</li>`).join("")}</ul>`;
  },

  _presenceDependencyText(proposal) {
    const count = proposal.exclusive_presence_samples;
    if (count == null) return "";
    if (!count)
      return " No labelled presence sample depends on this gate alone.";
    return ` ${count} labelled raw presence samples depend on this gate alone before hold; weakest energy ${proposal.weakest_exclusive_presence_energy}.`;
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
    return `<div class="evidence-conflict"><b>Occupied and empty readings overlap.</b>
      <p>No combination of gate thresholds can meet both raw-crossing targets for these labels. Device hold and filters are not included in this proof. ${this._esc(explanation)}</p>
      <p>Review the highlighted periods. Correct a label only if you know it is wrong; choose Unknown / exclude when occupancy is uncertain. If the empty label is correct (including dog-only activity), keep it: excluding it would hide false triggers.</p>
      <div class="review-periods">${periods.join(" ")}</div></div>`;
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
    const m = learning?.training;
    if (!m || !(m.present_samples || m.not_present_samples))
      return "<p>No human-labelled accuracy measurement yet. Record known occupied and empty periods to check this estimate.</p>";
    const items = [
      [
        "Missed presence",
        `${m.false_negatives ?? "—"} / ${m.present_samples}`,
        "Target: detect at least 99.9%",
      ],
      [
        "Triggered while labelled empty",
        `${m.false_positives ?? "—"} / ${m.not_present_samples}`,
        "Target: at most 0.5%",
      ],
      [
        "False-trigger events per hour",
        m.false_trigger_bursts_per_hour?.toFixed(2) ?? "—",
        "Target: at most 1",
      ],
    ];
    return `<div class="learning-metrics">${items.map(([label, value, target]) => `<div><span>${label}</span><b>${value}</b><small>${target}</small></div>`).join("")}</div>
      <p class="muted">These counts cover the combined device: any enabled gate can detect presence, and any gate can cause a false trigger. They measure the retained training data, not guaranteed future accuracy.</p>`;
  },

  _reviewPeriodHtml(period) {
    const state =
      period.state === "not_present"
        ? "Activity while labelled empty"
        : "Presence missed by the recommendation";
    const from = new Date(period.start * 1000).toLocaleString();
    const to = new Date(period.end * 1000).toLocaleTimeString();
    const focus =
      Object.entries(period.gates || {}).sort((a, b) => b[1] - a[1])[0]?.[0] ||
      "";
    const gates = Object.entries(period.gates || {})
      .map(
        ([key, peak]) => `${key.replace("g", "G").replace("_", " ")}: ${peak}`,
      )
      .join(" · ");
    return `<li><div><b>${state}</b><span>${this._esc(from)} – ${this._esc(to)} · ${period.samples} samples${period.observed_span_seconds != null ? ` · observations span ${period.observed_span_seconds}s; actual duration unknown` : ""}</span>${gates ? `<span>Peak energies: ${this._esc(gates)}</span>` : ""}</div><button type="button" data-action="review-period" data-start="${Number(period.start)}" data-end="${Number(period.end)}" data-state="${this._esc(period.state)}" data-gate-key="${this._esc(focus)}">Review on graph</button></li>`;
  },

  _reviewEvidenceHtml(learning) {
    const review = learning?.review;
    if (!review?.periods?.length) return "";
    const sessions = (review.sessions || [])
      .filter((item) => item.errors)
      .map(
        (item) =>
          `${new Date(item.start * 1000).toLocaleString()}: ${item.errors} / ${item.samples} ${item.state === "not_present" ? "empty" : "presence"} samples disagree`,
      );
    return `<details class="evidence-review" data-detail="evidence-review"><summary>Review ${review.period_count} ${review.period_count === 1 ? "period that disagrees with its label" : "periods that disagree with their labels"}</summary>
      <p>These are review suggestions, not proof of incorrect labels. Brief activity could be a person, a pet or noise; energy readings alone cannot tell which. No period below was automatically excluded.</p>
      <p>Open a period on the graph, check its times, then correct its status or choose Unknown / exclude. Learn again after saving.</p>
      <ul class="review-periods">${review.periods.map((period) => this._reviewPeriodHtml(period)).join("")}</ul>
      ${review.period_count > review.periods.length ? `<p>Showing the ${review.periods.length} largest periods, with space reserved for both label states.</p>` : ""}
      <details data-detail="review-sessions"><summary>By recording session</summary><ul>${sessions.map((text) => `<li>${this._esc(text)}</li>`).join("")}</ul></details></details>`;
  },

  _validationHtml(learning, currentTiming) {
    if (!learning) return "";
    const outcome = this._applyOutcome(learning, currentTiming);
    const backtest = learning.validation;
    const backtestHtml = backtest
      ? `<p>Earlier-data backtest: ${backtest.false_negatives ?? 0} missed presence samples; ${backtest.false_positives ?? 0} false triggers. The final recommendation uses all retained observations.</p>`
      : "";
    return `<section class="learning-report outcome-${outcome.level}" aria-label="Recommendation quality"><b>${outcome.label}</b>
      ${this._humanValidationHtml(learning)}${this._feasibilityHtml(learning)}${this._reviewEvidenceHtml(learning)}
      <details class="learning-diagnostics" data-detail="learning-diagnostics"><summary>Calculation details</summary>
      ${this._outlierFilterHtml(learning)}${backtestHtml}${this._falsePositiveSourcesHtml(learning)}${this._inferenceHtml(learning.automatic_evidence)}${this._learningWarningHtml(learning)}</details></section>`;
  },

  _applyOutcome(learning, currentTiming) {
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
      learning.method !== "human_priority_v7" ||
      learning.timing?.scope !== "reported_presence" ||
      this._timingChanged(learning, currentTiming) ||
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

  _timingChanged(learning, current) {
    const captured = learning?.timing?.configuration;
    return Boolean(
      captured &&
      current &&
      ["timeout", "on_delay", "off_delay"].some(
        (key) => (captured[key] ?? null) !== (current[key] ?? null),
      ),
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
    const scope =
      settings.timeout == null
        ? "Timeout is unavailable: results count raw threshold crossings. Your existing firmware remains supported."
        : settings.on_delay == null || settings.off_delay == null
          ? "Learning accounts for radar hold only. ESPHome filters are unknown; expose both delays to include them. The package is optional."
          : "Learning accounts for radar hold, then delayed on and delayed off. These are read-only inputs; Apply changes gate thresholds only.";
    const interval = timing?.sample_interval_seconds;
    const raw = learning?.raw_training;
    const changed = this._timingChanged(learning, current)
      ? "<p><b>Device timing has changed. Learn again to assess the current settings; the saved result still uses the values shown here.</b></p>"
      : "";
    return `<div class="notice timing-report"><b>${source}</b><div>${values}</div><p>${scope}</p>${changed}<details data-detail="timing-assumptions"><summary>Sampling and timing assumptions</summary>
      ${timing?.active ? `<p>Sampled estimate: consecutive high readings are treated as one run; empty-room spikes allow for activity between observations. Gaps and label changes restart the replay. ${learning.training?.timing_warmup_samples || 0} initial observations were left unscored because the preceding device state is unknown.</p>` : ""}
      ${interval != null ? `<p>Typical recorded interval: ${Number(interval)}s. These snapshots cannot establish sub-second spike lengths or exact detection times.</p>` : ""}
      ${raw && timing?.active ? `<p>Before hold and filters: ${raw.false_negatives} missed / ${raw.present_samples} presence observations; ${raw.false_positives} crossings / ${raw.not_present_samples} empty observations. The quality measurements below use the timing estimate.</p>` : ""}</details></div>`;
  },

  _recommendationsHtml(d, id) {
    const learning = d.last_learning;
    const outcome = this._applyOutcome(learning, d.timing_configuration);
    return `${this._nightlyReportHtml(d)}${this._savedResultsHtml(id, d)}<div class="controls"><button class="primary" data-action="learn">Learn thresholds</button><button class="apply-${outcome.level}" data-action="apply" ${outcome.enabled ? "" : "disabled"}>Apply learned thresholds · ${outcome.label}</button></div>
      <div class="muted">Learn creates a preview; Apply writes the selected values to the radar. You can apply a result even when its targets are not met.</div>
      ${this._timingHtml(learning, d.timing_configuration)}${this._validationHtml(learning, d.timing_configuration)}
      <details class="gate-results"><summary>Gate thresholds and sample counts</summary>${this._detailsHtml(d)}</details>`;
  },

  _dataActionsHtml() {
    return `<div class="export"><button data-action="json">Export JSON</button><button data-action="csv">Export CSV</button></div>
      <div class="data-clear"><span class="muted">Remove recorded history, labels and recommendations for this device.</span><button data-action="clear">Clear data</button></div>`;
  },
};
