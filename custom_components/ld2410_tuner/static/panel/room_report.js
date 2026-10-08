export const panelRoomReport = {
  _roomReportHtml(report, members) {
    if (!report.room) return "";
    const date = (value) => new Date(value * 1000).toLocaleString();
    const time = (value) => `${Number(value || 0).toFixed(1)}s`;
    const individual = Object.entries(report.devices || {})
      .map(
        ([id, item]) =>
          `<tr><th>${this._esc(members[id]?.name || "Unavailable radar")}</th><td>${this._scoreText(item)}</td><td>${time(Object.values(item.outcomes || {}).reduce((sum, source) => sum + (source.present?.active_seconds || 0), 0))} total occupied time covered; ${time(item.exclusive_presence_seconds)} occupied time covered by this radar alone</td></tr>`,
      )
      .join("");
    const timing = Object.entries(report.timing || {})
      .map(
        ([id, config]) =>
          `<p><b>${this._esc(members[id]?.name || "Unavailable radar")}:</b> ${this._esc(this._timingPolicyNote({ configuration: config, scope: config.scope }) || "Timing between observations remains estimated.")}</p>`,
      )
      .join("");
    return `<div class="room-report"><p><b>Combined score ${this._scoreText(report.room)}</b> ${report.room.basis === "estimated" ? "· Includes automatic estimates; not independent validation" : ""}</p>
      ${this._roomOutcomeHtml(report.room)}
      <p class="muted">${this._esc(date(report.start))} – ${this._esc(date(report.end))} · Stored room scorer v${this._esc(report.scorer_version)}</p>
      <p>Shared recording coverage: ${time(report.shared_recording_seconds)}. Excluded: ${time(report.excluded?.no_shared_recording_seconds)} without recordings from every member (including timing warm-up), and ${time(report.excluded?.unlabelled_seconds)} without usable occupancy labels.</p>
      <div class="table-scroll"><table class="comparison-table"><thead><tr><th>Radar</th><th>Score alone on group evidence</th><th>Contribution</th></tr></thead><tbody>${individual}</tbody></table></div>
      <div class="muted">Contributions and scores use this group's evidence. They are not the device-card scores. Overlapping detections count once.</div>
      ${timing}
    </div>`;
  },

  _roomOutcomeHtml(result) {
    const metrics = this._roomMetricsText(result);
    const outcomes = Object.entries(result.outcomes || {})
      .map(([source, states]) => {
        const p = states.present,
          n = states.not_present;
        return `<p><b>${source === "human" ? "Human labels" : "Automatic estimates"}:</b> ${p.hits} detected / ${p.samples - p.hits} missed occupied observations; ${n.hits} false triggers / ${n.samples - n.hits} correct empty observations. ${p.seconds.toFixed(1)}s occupied, ${(p.seconds - p.active_seconds).toFixed(1)}s conservatively missed; ${n.seconds.toFixed(1)}s empty, ${n.active_seconds.toFixed(1)}s potentially false-active.</p>`;
      })
      .join("");
    return `<p>${metrics}</p>${outcomes}`;
  },

  _roomMetricsText(result) {
    if (result.score == null)
      return this._esc(result.reason || "Not enough evidence");
    const target = result.target_met
      ? "99.9% occupied-time target met in replay."
      : "99.9% occupied-time target not met in replay.";
    return `Estimated occupied time detected: ${(100 * result.presence_recall).toFixed(3)}%. False-active empty time: ${Number(result.false_positive_percent).toFixed(3)}%. ${target}`;
  },
};
