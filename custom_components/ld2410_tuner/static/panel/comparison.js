import { SLOT_LABELS } from "./saved_results.js";

const LABELS = { live: "Live settings", ...SLOT_LABELS };

export const panelComparison = {
  _scoreText(pattern) {
    return pattern?.score == null
      ? "—"
      : `${Number(pattern.score).toFixed(2)}/100`;
  },

  _comparisonHeaderHtml(d) {
    const report = d.comparison;
    const live = report?.patterns?.live;
    const best = report?.patterns?.[report?.best_slots?.[0]];
    const busy = ["pending", "running"].includes(report?.state);
    return `<button type="button" class="comparison-header ${busy ? "is-busy" : ""}" data-action="review-comparison" title="Compare stored scores for the current and saved patterns">Current ${this._scoreText(live)} · Best ${this._scoreText(best)}${report?.stale ? " · refreshing" : ""}</button>`;
  },

  _comparisonHtml(id, d) {
    const report = d.comparison || {};
    const localError = this._comparisonErrors.get(id);
    const busy =
      this._comparisonRequests.has(id) ||
      (!localError && ["pending", "running"].includes(report.state));
    const reason = localError || report.reason;
    const rows = Object.entries(LABELS)
      .map(([slot, label]) => this._comparisonRow(slot, label, report))
      .join("");
    const dates = report.mixed_evidence ? " · Different evaluation dates" : "";
    const stored = report.scorer_version
      ? `Stored scores · Scorer v${report.scorer_version}${dates}`
      : "No stored assessment yet";
    return `<div class="pattern-comparison"><div class="comparison-heading"><b>Compare saved patterns</b><button data-action="refresh-comparison" ${busy ? "disabled" : ""}>Check stored scores</button></div>
      <div class="muted ${busy ? "is-busy" : ""}" role="status">${busy ? "Calculating missing scores…" : this._esc(reason || stored)}</div>
      ${report.patterns ? `<div class="table-scroll"><table class="comparison-table"><thead><tr><th>Pattern</th><th>Score</th><th>Evidence & outcomes</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>` : ""}
      <details data-detail="comparison-score"><summary>What the score means</summary><p>100 means no measured errors in these recordings. Each 1% of missed occupied time costs 5 points; each 1% of false-active empty time costs 1 point. The score stops at zero. It is a comparison score, not a probability or a guarantee of accuracy.</p><p>Each score uses bounded evidence (up to 5,000 observations per state and source), active gates, outlier filtering and the timing policy at calculation time. The conservative occupied-time estimate is scored when onset timing is uncertain. Any gate can detect presence; any gate can cause a false positive. Human labels take priority; automatic estimates fill missing evidence and break ties.</p><p>Scores are stored with their scorer version. New recordings, edited labels and timing-policy changes do not recalculate an existing score. A changed scorer recalculates scores once. Check stored scores reuses valid scores and retries unscored assessments. Different evaluation dates may use different recordings or timing; this is not a controlled comparison on identical evidence. See each row’s sample outcomes for its timing assumptions and exclusions.</p><p>Best is the lowest stored error cost among compatible saved patterns, with the learner’s duration tie-breaks. A score alone does not establish the 99.9% presence target. Selecting a pattern does not apply it.</p></details></div>`;
  },

  _comparisonRow(slot, label, report) {
    const item = report.patterns?.[slot];
    const best = report.best_slots?.includes(slot);
    const selectable = slot !== "live" && item?.result_id;
    const note =
      slot !== "live" && item?.result_id && !item.applicable
        ? " · incompatible; learn again to apply"
        : "";
    const review = selectable
      ? `<button data-action="select-comparison" data-slot="${slot}">Review</button>`
      : "";
    return `<tr data-pattern="${slot}"><th>${label}${best ? ' <span class="pill">Best</span>' : ""}</th><td class="comparison-score">${this._scoreText(item)}</td><td>${this._comparisonMetrics(item, slot)}${this._esc(note)}${this._storedScoreStamp(item)}</td><td>${review}</td></tr>`;
  },

  _storedScoreStamp(item) {
    if (!item?.evaluated_at) return "";
    const date = new Date(item.evaluated_at * 1000).toLocaleString();
    return `<div class="muted">Scorer v${this._esc(item.scorer_version)} · ${this._esc(date)}</div>`;
  },

  _scoreEvidenceHtml(item) {
    const note = this._timingPolicyNote({
      configuration: item.timing,
      scope: item.timing?.scope,
    });
    const excluded =
      Number(item.outliers?.human?.excluded?.present || 0) +
      Number(item.outliers?.automatic?.excluded?.present || 0);
    return `<p>${this._esc(note || "Timing between recordings is an estimate.")}</p><p>${Number(item.incomplete_samples || 0)} incomplete observations and ${excluded} isolated occupied-state outliers excluded.</p>`;
  },

  _comparisonMetrics(item, slot) {
    if (item?.score == null)
      return this._esc(item?.reason || "No saved result");
    const evidence =
      item.basis === "human"
        ? "Human labelled"
        : "Includes automatic estimates";
    const recall = (100 * item.presence_recall).toFixed(3);
    const falseActive = Number(item.false_positive_percent).toFixed(3);
    const samples = ["human", "automatic"]
      .map((source) => {
        const m = item[source];
        if (!m) return "";
        return `${source === "human" ? "Human" : "Automatic"}: ${(m.present_samples || 0) - (m.false_negatives || 0)} hits, ${m.false_negatives ?? 0} misses, ${m.false_positives ?? 0} false triggers, ${(m.not_present_samples || 0) - (m.false_positives || 0)} correct empty readings. ${Number(m.duration?.present_seconds || 0).toFixed(1)}s occupied / ${Number(m.duration?.empty_seconds || 0).toFixed(1)}s empty observed; up to ${Number(m.duration?.missed_seconds_upper || 0).toFixed(1)}s missed, ${Number(m.duration?.false_positive_seconds || 0).toFixed(1)}s false-active.`;
      })
      .join(" ");
    return `${evidence}<br>Presence time ${recall}% · False-active ${falseActive}%<br><span class="muted">${item.target_met ? "99.9% presence target met in replay" : "99.9% presence target not met"}</span><details data-detail="comparison-${slot}"><summary>Sample outcomes</summary>${this._esc(samples)} Samples support the assessment; elapsed time determines the score.${this._scoreEvidenceHtml(item)}</details>`;
  },

  _ensureComparisons() {
    for (const [id, d] of Object.entries(this._data?.devices || {})) {
      if (d.comparison?.state === "pending" && !this._comparisonErrors.has(id))
        void this._requestComparison(id);
    }
  },

  async _requestComparison(id, force = false) {
    if (this._comparisonRequests.has(id)) return;
    this._comparisonRequests.add(id);
    this._comparisonErrors.delete(id);
    try {
      await this._call("compare_results", { device_id: id, force }, 120000);
    } catch (error) {
      this._comparisonErrors.set(
        id,
        `Comparison unavailable: ${error?.message || error}. Use Check stored scores to retry.`,
      );
    } finally {
      this._comparisonRequests.delete(id);
    }
    if (this.isConnected) await this._load(true);
  },

  _wireComparison(card, id) {
    card.querySelector('[data-action="review-comparison"]').onclick = (
      event,
    ) => {
      event.stopPropagation();
      this._openRecommendations(card, id);
    };
    const refresh = card.querySelector('[data-action="refresh-comparison"]');
    refresh.onclick = () => {
      refresh.disabled = true;
      refresh.classList.add("is-busy");
      void this._requestComparison(id, true);
    };
    for (const button of card.querySelectorAll(
      '[data-action="select-comparison"]',
    )) {
      button.onclick = () => {
        this._learningSelection.set(id, button.dataset.slot);
        this._draw();
        this._renderChartCanvas(id, true);
      };
    }
  },
};
