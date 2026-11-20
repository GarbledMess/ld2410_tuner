// "Radar timeout": every Learn scores this radar's timeouts on its own and its rooms' labelled
// presence and empty time at its live thresholds. Where automatic timeouts are on, Learn sets a cheaper one on the
// radar; the next Learn tunes the thresholds for it.
function minutes(seconds) {
  return `${(Number(seconds || 0) / 60).toFixed(1)}m`;
}

const MODES = {
  off: "Off",
  overnight: "Overnight learning only",
  all: "Every completed Learn",
};

export const panelTimeoutSuggestion = {
  _timeoutApplyPolicyText(mode) {
    if (mode === "all")
      return "Every completed Learn can set a cheaper timeout on the radar, including manual Learn.";
    if (mode === "overnight")
      return "Only overnight learning can set a cheaper timeout on the radar.";
    return "Automatic timeouts are off. Learn suggests a timeout; set it on the radar yourself.";
  },

  _timeoutStatus(found) {
    const suggestion = found?.suggestion;
    const now = found?.current_timeout;
    const share =
      suggestion?.keep == null
        ? ""
        : `, the shortest that keeps ${Math.round(suggestion.keep * 100)}% of the best timeout's improvement`;
    if (!found)
      return "Learn scores this radar's timeouts on its own and its rooms' labelled presence and empty time at its live thresholds.";
    if (found.status === "error")
      return found.reason || "Could not check timeouts.";
    if (found.status === "applied")
      return `Learn set the radar's timeout to ${found.applied_timeout} s (was ${found.previous_timeout} s)${share}. The next Learn tunes the thresholds for it.`;
    if (!suggestion)
      return "This radar, or a room it is in, needs labelled presence before Learn can suggest a timeout.";
    if (found.status === "kept")
      return `The radar keeps its timeout (${now} s)${share}.`;
    const reason = found.reason ? ` ${found.reason}` : "";
    const current = now == null ? "" : ` (now ${now} s)`;
    return `Suggested: ${suggestion.timeout} s${current}${share}.${reason}`;
  },

  _timeoutSuggestionHtml(id, d) {
    const found = d?.timeout_suggestion;
    const suggestion = found?.suggestion;
    const applied = found?.status === "applied";
    const now = applied ? found.previous_timeout : found?.current_timeout;
    const rows = (suggestion?.candidates || [])
      .map((row) => {
        const marks = [
          row.timeout === suggestion.timeout
            ? '<span class="pill">Suggested</span>'
            : "",
          suggestion.cheapest != null &&
          row.timeout === suggestion.cheapest &&
          row.timeout !== suggestion.timeout
            ? '<span class="pill">Cheapest</span>'
            : "",
          now != null && Number(row.timeout) === Number(now)
            ? `<span class="pill">${applied ? "Was" : "Now"}</span>`
            : "",
        ].join(" ");
        return `<tr><th>${this._esc(row.timeout)} s ${marks}</th><td>${this._esc(row.dark_spells)}</td><td>${minutes(row.missed_seconds)}</td><td>${minutes(row.false_seconds)}</td><td>${this._esc(row.false_starts)}</td></tr>`;
      })
      .join("");
    const table = rows
      ? `<div class="table-scroll"><table class="timeout-table"><thead><tr><th>Timeout</th><th>Dark spells</th><th>Missed</th><th>False light</th><th>False starts</th></tr></thead><tbody>${rows}</tbody></table></div>`
      : "";
    const policy = d?.timeout_apply_policy || {
      override: "inherit",
      global: "off",
      effective: "off",
    };
    const chosen = this._pickerValue(`timeout-apply:${id}`, policy.override);
    const options = {
      inherit: `Inherit global (${MODES[policy.global]})`,
      ...MODES,
    };
    const picker = `<label class="saved-result-picker">Automatic timeout for this sensor<select data-action="device-timeout-apply">${Object.entries(
      options,
    )
      .map(
        ([value, label]) =>
          `<option value="${value}" ${chosen === value ? "selected" : ""}>${this._esc(label)}</option>`,
      )
      .join(
        "",
      )}</select></label> <button type="button" data-action="device-timeout-apply-save">Save automatic timeout</button><div class="muted">${this._esc(this._timeoutApplyPolicyText(policy.effective))}</div>`;
    return `<div class="timeout-suggestion"><div class="comparison-heading"><b>Radar timeout</b></div>
      <div class="muted" role="status">${this._esc(this._timeoutStatus(found))}</div>${table}${picker}</div>`;
  },

  _wireTimeoutSuggestion(card, id) {
    const override = card.querySelector('[data-action="device-timeout-apply"]');
    if (!override) return;
    this._wirePicker(
      override,
      card.querySelector('[data-action="device-timeout-apply-save"]'),
      `timeout-apply:${id}`,
      (mode) =>
        this._call("configure_device_timeout_apply", { device_id: id, mode }),
    );
  },
};
