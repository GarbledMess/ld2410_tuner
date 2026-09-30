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
      <div>Rare, separated low readings in brief dips are excluded before learning. The same retained observations determine thresholds, time-based accuracy and recommendation quality.</div>
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
    return `<div class="evidence-conflict"><b>Earlier sample-based assessment: occupied and empty readings overlap.</b>
      <p>Under the earlier model’s sample limits, no gate combination met both raw-crossing targets. This does not assess the current time-based cost, radar hold or firmware filters. ${this._esc(explanation)}</p>
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
    return `<div class="notice">Automatic evidence: ${inferred.samples?.present || 0} present / ${inferred.samples?.not_present || 0} empty estimates. Mean confidence: ${presentConfidence} / ${absentConfidence}. Legacy weighted sample totals (diagnostics only): ${(inferred.effective_weight?.present || 0).toFixed(1)} / ${(inferred.effective_weight?.not_present || 0).toFixed(1)}. Human-labelled results always take priority.${deferred}</div>`;
  },

  _missedPresenceText(measured) {
    const uncertainty = measured?.onset_uncertainty;
    return uncertainty?.samples
      ? `${uncertainty.misses_if_earliest_onset}–${uncertainty.misses_if_latest_onset}`
      : `${measured?.false_negatives ?? "—"}`;
  },

  _humanValidationHtml(learning) {
    const m = learning?.training;
    if (m?.duration)
      return (
        this._durationValidationHtml(m.duration) + this._sampleOutcomesHtml(m)
      );
    if (!m || !(m.present_samples || m.not_present_samples))
      return "<p>No human-labelled accuracy measurement yet. Record known occupied and empty periods to check this estimate.</p>";
    return `<p class="notice">Saved sample-based measurements. Learn again for the current time-based score and 99.9% occupied-time goal.</p>${this._sampleOutcomesHtml(m)}
      <p class="muted">Recorded false-trigger events per hour: ${m.false_trigger_bursts_per_hour?.toFixed(2) ?? "—"}. This historical diagnostic is not a current fixed frequency limit.</p>`;
  },

  _sampleOutcomesHtml(m, automatic = false) {
    if (!m) return "";
    const uncertainty = m.onset_uncertainty;
    const missedLow =
      uncertainty?.misses_if_earliest_onset ?? m.false_negatives;
    const missedHigh = uncertainty?.misses_if_latest_onset ?? m.false_negatives;
    const remaining = (total, missed) =>
      Number.isFinite(total) && Number.isFinite(missed) ? total - missed : null;
    const items = [
      [
        "Presence detected (hits)",
        remaining(m.present_samples, missedHigh),
        remaining(m.present_samples, missedLow),
        m.present_samples,
      ],
      ["Presence missed", missedLow, missedHigh, m.present_samples],
      [
        "Correctly empty",
        remaining(m.not_present_samples, m.false_positives),
        remaining(m.not_present_samples, m.false_positives),
        m.not_present_samples,
      ],
      [
        "False presence",
        m.false_positives,
        m.false_positives,
        m.not_present_samples,
      ],
    ];
    const basis =
      m.basis === "sampled_timing_estimate"
        ? "Counts after modeled timeout and known delays; timing warm-up is excluded. Ranges reflect unresolved transitions."
        : "Raw threshold-crossing counts.";
    return `<div class="sample-outcome-report"><b>${automatic ? "Automatically labelled" : "Human-labelled"} snapshot outcomes</b>
      <div class="sample-outcomes">${items.map(([label, low, high, total]) => `<div><span>${label}</span><b>${this._sampleRange(low, high)} / ${Number.isFinite(total) ? total : "—"}</b></div>`).join("")}</div>
      <p class="muted">${basis} These counts help assess the recording; the time-based cost chooses thresholds. Counts alone do not make a learning job fail.</p></div>`;
  },

  _sampleRange(low, high) {
    if (!Number.isFinite(low) || !Number.isFinite(high)) return "—";
    return low === high ? String(low) : `${low}–${high}`;
  },

  _secondsText(value) {
    if (value == null) return "—";
    return value < 60
      ? `${Number(value).toFixed(1)}s`
      : `${(value / 60).toFixed(1)} min`;
  },

  _secondsRange(low, high) {
    const start = this._secondsText(low);
    const end = this._secondsText(high ?? low);
    return start === end ? start : `${start}–${end}`;
  },

  _recallText(duration) {
    if (duration?.presence_recall == null) return "—";
    const high = (100 * duration.presence_recall).toFixed(3);
    const low = (
      100 * (duration.presence_recall_lower ?? duration.presence_recall)
    ).toFixed(3);
    return low === high ? `${high}%` : `${low}–${high}%`;
  },

  _durationValidationHtml(m, automatic = false) {
    const score =
      m.false_positive_score == null
        ? "—"
        : Number(m.false_positive_score).toFixed(3);
    const items = [
      [
        "Presence-time recall",
        this._recallText(m),
        "Target: at least 99.9% of observed occupied time",
      ],
      [
        "False-positive score",
        score,
        "0 is best; −1 means false-active for 1% of observed empty time",
      ],
      [
        "False-active time",
        `${this._secondsText(m.false_positive_seconds)} / ${this._secondsText(m.empty_seconds)}`,
        "All enabled gates combined; overlapping triggers count once",
      ],
    ];
    return `<div class="learning-metrics">${items.map(([label, value, note]) => `<div><span>${label}</span><b>${value}</b><small>${note}</small></div>`).join("")}</div>
      <p>Estimated missed presence: ${this._secondsRange(m.missed_seconds, m.missed_seconds_upper)} of ${this._secondsText(m.present_seconds)} occupied time. Longest estimated gap: ${this._secondsRange(m.longest_missed_seconds, m.longest_missed_seconds_upper)}. Completely missed occupied periods: ${m.missed_presence_episodes}.</p>
      ${m.error_cost != null ? `<p class="muted">${automatic ? "Unweighted recorded-time cost" : "Search error cost"}: ${Number(m.error_cost).toFixed(3)} (lower is better). Missed occupied-time percentage costs ${Number(m.missed_time_cost)}× false-active empty-time percentage, using the conservative missed-time estimate. The 99.9% target remains the quality goal; crossing it does not make extra misses free.${automatic ? " Automatic search additionally weights time by confidence, after human evidence." : ""}</p>` : ""}
      <p class="muted">Scores use elapsed time within recorded sessions, not the number of readings. Missing history is not counted as observed time. Timing between snapshots is estimated; empty-time penalties use conservative activity intervals. These are training results, not guaranteed field accuracy.</p>
      ${m.unscored_presence_samples || m.unscored_empty_samples ? `<p>${m.unscored_presence_samples || 0} occupied and ${m.unscored_empty_samples || 0} empty observations have no measurable duration after gaps or timing warm-up.</p>` : ""}`;
  },

  _influenceHtml(periods = []) {
    if (!periods.length) return "";
    const items = periods.slice(0, 3).map((p) => {
      const state =
        p.state === "not_present" ? "false-active" : "missed-presence";
      const time = `${new Date(p.start * 1000).toLocaleString()} – ${new Date(p.end * 1000).toLocaleString()}`;
      const comparison =
        p.score_without_period == null
          ? ""
          : ` At the same thresholds, the empty-time score without this period would be ${Number(p.score_without_period).toFixed(3)}.`;
      return `<li><div><b>Check this period first</b><span>${this._esc(time)}</span><p>${Number(p.error_share_percent).toFixed(1)}% of estimated ${state} time comes from this period (${this._secondsText(p.error_seconds)}).${comparison}</p></div><button type="button" data-action="review-period" data-start="${Number(p.start)}" data-end="${Number(p.end)}" data-state="${p.state}">Review on graph</button></li>`;
    });
    return `<div class="notice evidence-influence"><b>Periods driving this result</b><p>A label or recording problem here could explain the poor score. Check these periods first, but keep correctly labelled pet-only activity: it is real negative evidence. Nothing has been removed; the comparison keeps the thresholds fixed and does not rerun learning.</p><ul class="review-periods">${items.join("")}</ul></div>`;
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
    const influence = this._influenceHtml(review?.influence);
    if (!review?.periods?.length) return influence;
    const sessions = (review.sessions || [])
      .filter((item) => item.errors)
      .map(
        (item) =>
          `${new Date(item.start * 1000).toLocaleString()}: ${item.errors} / ${item.samples} ${item.state === "not_present" ? "empty" : "presence"} samples disagree`,
      );
    return `${influence}<details class="evidence-review" data-detail="evidence-review"><summary>Review ${review.period_count} ${review.period_count === 1 ? "period that disagrees with its label" : "periods that disagree with their labels"}</summary>
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
    const backtestHtml = backtest?.duration
      ? `<p>Earlier-data backtest: presence-time recall ${this._recallText(backtest.duration)}; false-positive score ${backtest.duration.false_positive_score?.toFixed(3) ?? "—"}. The final recommendation uses all retained observations.</p>`
      : backtest
        ? `<p>Earlier-data backtest: ${this._missedPresenceText(backtest)} missed presence samples; ${backtest.false_positives ?? 0} false triggers. The final recommendation uses all retained observations.</p>`
        : "";
    return `<section class="learning-report outcome-${outcome.level}" aria-label="Recommendation quality"><b>${outcome.label}</b>
      ${learning.method !== "human_priority_v10" ? '<p class="notice">This saved result used an older learning model. Its recorded scores have not been recalculated. Learn again before applying it with the current model.</p>' : ""}
      ${this._humanValidationHtml(learning)}${this._feasibilityHtml(learning)}${this._reviewEvidenceHtml(learning)}
      <details class="learning-diagnostics" data-detail="learning-diagnostics"><summary>Calculation details</summary>
      ${this._outlierFilterHtml(learning)}${backtestHtml}${learning.estimated_training?.duration && learning.automatic_evidence?.used ? `<h4>Automatically labelled time (lower-confidence estimates)</h4>${this._durationValidationHtml(learning.estimated_training.duration, true)}${this._sampleOutcomesHtml(learning.estimated_training, true)}` : ""}${this._falsePositiveSourcesHtml(learning)}${this._inferenceHtml(learning.automatic_evidence)}${this._learningWarningHtml(learning)}</details></section>`;
  },

  _applyOutcome(learning, currentTiming) {
    if (!Object.keys(learning?.proposals || {}).length)
      return { level: "none", label: "No learned results", enabled: false };
    if (learning.method !== "human_priority_v10")
      return {
        level: "caution",
        label: "Older learning model · learn again",
        enabled: true,
      };
    if (this._timingChanged(learning, currentTiming))
      return {
        level: "caution",
        label: "Device timing changed or policy updated · learn again",
        enabled: true,
      };
    if (learning.training?.duration)
      return this._durationOutcome(learning, currentTiming);
    if (
      learning.status === "unsafe" ||
      learning.feasibility?.status === "conflict"
    )
      return { level: "bad", label: "Targets not met", enabled: true };
    const measured = (metrics) =>
      metrics?.present_samples > 0 && metrics?.not_present_samples > 0;
    if (
      learning.status !== "ok" ||
      learning.method !== "human_priority_v10" ||
      this._timingNeedsReview(learning) ||
      this._timingChanged(learning, currentTiming) ||
      !measured(learning.training) ||
      !measured(learning.validation) ||
      this._backtestHasIssues(learning)
    )
      return { level: "caution", label: "Review concerns", enabled: true };
    return { level: "good", label: "Targets met", enabled: true };
  },

  _durationOutcome(learning, currentTiming) {
    if (learning.status === "unsafe")
      return {
        level: "bad",
        label: "Presence gaps or continuous false activity",
        enabled: true,
      };
    if (learning.status === "uncertain")
      return {
        level: "caution",
        label: this._estimatedOutcomeLabel(learning),
        enabled: true,
      };
    if (learning.status === "tradeoff")
      return {
        level: "caution",
        label: "Presence target met · review false-active time",
        enabled: true,
      };
    const measured = (m) =>
      m?.duration?.present_seconds > 0 && m?.duration?.empty_seconds > 0;
    const concerns =
      learning.status !== "ok" ||
      learning.method !== "human_priority_v10" ||
      this._timingNeedsReview(learning) ||
      !measured(learning.training) ||
      !measured(learning.validation) ||
      this._backtestHasIssues(learning) ||
      this._timingChanged(learning, currentTiming);
    return {
      level: concerns ? "caution" : "good",
      label: concerns ? "Review concerns" : "Presence target met",
      enabled: true,
    };
  },

  _estimatedOutcomeLabel(learning) {
    const human = learning.training?.duration || {};
    const automatic = learning.estimated_training?.duration || {};
    const presence = human.presence_recall != null ? human : automatic;
    const falseActive =
      human.false_positive_percent ?? automatic.false_positive_percent;
    const parts = [];
    if (presence.presence_recall != null)
      parts.push(`Recall ${this._recallText(presence)}`);
    if (falseActive != null)
      parts.push(`false-active ${Number(falseActive).toFixed(2)}%`);
    return parts.length
      ? `Estimated · ${parts.join(" · ")}`
      : "Limited recorded time · review result";
  },

  _backtestHasIssues(learning) {
    const metrics = learning.validation;
    const targets = learning.targets || {};
    if (metrics?.duration) {
      const m = metrics.duration;
      return (
        m.presence_recall == null ||
        m.presence_recall_lower < (targets.sensitivity ?? 0.999) ||
        m.missed_presence_episodes > 0 ||
        m.false_positive_percent >= 100
      );
    }
    if (!metrics) return true;
    if (metrics.onset_uncertainty?.samples) return true;
    if (metrics.sensitivity < (targets.sensitivity ?? 0.999)) return true;
    return Object.entries({
      false_positive_rate: 0.005,
      missed_presence_episodes: 0,
      longest_missed_run_samples: 1,
      false_trigger_bursts_per_hour: 1,
    }).some(([key, fallback]) => metrics[key] > (targets[key] ?? fallback));
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
    const scope =
      settings.mode === "disabled"
        ? "Timing adjustments are disabled. Scores use raw threshold activity without timeout or on/off delays. Apply still changes gate thresholds only."
        : settings.timeout == null
          ? "Timeout is unavailable: time scores estimate raw threshold activity between snapshots. Your existing firmware remains supported."
          : settings.on_delay == null || settings.off_delay == null
            ? "Learning accounts for radar hold only. ESPHome filters are unknown; expose both delays or configure global fallback values to include them. The package is optional."
            : "Learning accounts for radar hold, then delayed on and delayed off. These are read-only inputs; Apply changes gate thresholds only.";
    const uncertainty = learning?.training?.onset_uncertainty;
    const uncertaintyHtml = uncertainty?.samples
      ? `<p><b>${Number(uncertainty.samples)} presence observations have unresolved transition timing.</b> The signal recovered between snapshots, so the actual transition and any on delay may precede the high snapshot. The missed-presence range shows both possible onset times. These observations are retained. Their conservative missed-time estimate contributes to the finite error cost; uncertainty does not impose an absolute threshold requirement.</p>`
      : "";
    const interval = timing?.sample_interval_seconds;
    const raw = learning?.raw_training;
    const changed = this._timingChanged(learning, current)
      ? "<p><b>Device timing has changed or the global timing policy was updated. Learn again to assess the current settings; the saved result still uses the values shown here.</b></p>"
      : "";
    return `<div class="notice timing-report"><b>${source}</b><div>${values}</div><p>${scope}</p>${this._timingPolicyNote(timing) ? `<p>${this._esc(this._timingPolicyNote(timing))}</p>` : ""}${changed}${learning?.status === "uncertain" ? "<p>Sampling uncertainty: recordings do not establish every transition time, and very short periods may have no measurable duration. This can remain even when all three device timing settings are known. The estimates and sample counts below show what was measured.</p>" : ""}${uncertaintyHtml}<details data-detail="timing-assumptions"><summary>Sampling and timing assumptions</summary>
      ${timing?.active ? `<p>Sampled estimate: consecutive high readings are treated as one run; empty-room spikes allow for activity between observations. Gaps and label changes restart the replay. ${learning.training?.timing_warmup_samples || 0} initial observations were left unscored because the preceding device state is unknown.</p>` : ""}
      ${interval != null ? `<p>Typical recorded interval: ${Number(interval)}s. These snapshots cannot establish sub-second spike lengths or exact detection times.</p>` : ""}
      ${raw && timing?.active ? `<p>Before hold and filters: ${raw.false_negatives} missed / ${raw.present_samples} presence observations; ${raw.false_positives} crossings / ${raw.not_present_samples} empty observations. The quality measurements below use the timing estimate.</p>` : ""}</details></div>`;
  },

  _recommendationsHtml(d, id) {
    const learning = d.last_learning;
    const outcome = this._applyOutcome(learning, d.timing_configuration);
    return `<div class="recovery-progress">${this._recoveryHtml(d)}</div>${this._nightlyReportHtml(d)}${this._savedResultsHtml(id, d)}${this._comparisonHtml(id, d)}<div class="controls"><button class="primary" data-action="learn">Learn thresholds</button><button class="apply-${outcome.level}" data-action="apply" ${outcome.enabled ? "" : "disabled"}>Apply learned thresholds · ${outcome.label}</button></div>
      <div class="muted">Manual Learn creates a preview; Apply writes the selected values to the radar. Overnight runs can automatically apply improvements when enabled in global settings. You can apply a result even when its targets are not met.</div>
      ${this._timingHtml(learning, d.timing_configuration)}${this._validationHtml(learning, d.timing_configuration)}
      <details class="gate-results"><summary>Gate thresholds and sample counts</summary>${this._detailsHtml(d)}</details>`;
  },

  _dataActionsHtml() {
    return `<div class="export"><button data-action="json">Export JSON</button><button data-action="csv">Export CSV</button></div>
      <div class="data-clear"><span class="muted">Remove recorded history, labels and recommendations for this device.</span><button data-action="clear">Clear data</button></div>`;
  },
};
