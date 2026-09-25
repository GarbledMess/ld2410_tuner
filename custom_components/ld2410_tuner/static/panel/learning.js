export const panelLearning = {
  _learningWarningHtml(learning) {
    if (!learning?.warnings?.length) return "";
    const style = learning.status === "unsafe" ? "warn" : "";
    return `<div class="notice ${style}">${learning.warnings.map(this._esc).join("<br>")}</div>`;
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
    return `<div class="notice">Human-labelled timed observations: <b>${validation.false_negatives ?? "—"} missed / ${validation.present_samples} presence samples</b> (${sensitivity}; target 99.9%). Missed episodes: ${validation.missed_presence_episodes ?? "—"}; longest missed run: ${validation.longest_missed_run_samples ?? "—"} samples. False-trigger bursts: ${validation.false_trigger_bursts ?? "—"} (${falseRate}). ${recommendation}</div>`;
  },

  _validationHtml(learning) {
    const backtest = learning?.validation;
    const backtestHtml = backtest
      ? `<div class="notice">Earlier-data backtest: ${backtest.false_negatives ?? 0} missed presence samples; ${backtest.false_positives ?? 0} false triggers. The final recommendation uses all observations.</div>`
      : "";
    return (
      this._humanValidationHtml(learning) +
      backtestHtml +
      this._falsePositiveSourcesHtml(learning) +
      this._inferenceHtml(learning?.automatic_evidence)
    );
  },

  _actionsHtml(learning, validationHtml) {
    const warning = this._learningWarningHtml(learning);
    return `${validationHtml}${warning}<div class="controls"><button class="primary" data-action="learn">Learn thresholds</button><button data-action="apply" ${learning?.status === "ok" && learning?.method === "human_priority_v4" ? "" : "disabled"}>Apply recommended thresholds</button><button data-action="clear">Clear data</button></div><div class="export"><button data-action="json">Export JSON</button><button data-action="csv">Export CSV</button></div>`;
  },
};
