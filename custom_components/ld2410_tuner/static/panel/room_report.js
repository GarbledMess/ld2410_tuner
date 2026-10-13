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

  // ---- Scoreboard (spec 6) ----------------------------------------------
  // A card under a group shows this radar's role in that group's latest
  // assessment; the individual learner score moves into "Radar on its own".

  // The group whose role line this card shows: only when the radar is one of
  // its members, so a leaf's fallback radars keep the individual headline.
  _memberGroup(id, groupId) {
    const group = this._roomsData().groups[groupId];
    return group?.device_ids?.includes(id) ? group : null;
  },

  _roomRoleHtml(id, group) {
    const role = group.assessment?.devices?.[id];
    const figures = role
      ? [
          `Covers alone ${this._shareText(role.exclusive_presence_seconds, role.occupied_seconds)}`,
          `False-active ${this._shareText(role.false_active_seconds, role.empty_seconds)}`,
          `Missed what it saw ${this._missedSeenText(role)}`,
        ].join(" · ")
      : "Not assessed yet";
    return `<div class="room-role"><b>${this._esc(group.name)}</b> · ${this._esc(figures)}</div>`;
  },

  _ownScoreHtml(d) {
    return `<details class="own-score" data-detail="own-score"><summary>Radar on its own</summary><p class="muted">Individual learner score on this radar's own labels: a different measure, not comparable with room scores.</p>${this._comparisonHeaderHtml(d)}</details>`;
  },

  // One decimal throughout, trailing ".0" dropped (71%, 0.4%). Only exactly
  // 100% shows "100%"; non-zero shares under 0.1% show "<0.1%".
  _shareText(part, whole) {
    if (!(whole > 0)) return "—";
    const percent = (100 * part) / whole;
    if (percent > 0 && percent < 0.1) return "<0.1%";
    const shown = percent < 100 ? Math.min(percent, 99.9) : percent;
    return `${Number(shown.toFixed(1))}%`;
  },

  _missedSeenText(role) {
    const missed = role.missed_seen_seconds;
    if (missed == null) return "—";
    const parts = [
      [role.missed_seen_inferred_seconds, "inferred"],
      [role.missed_seen_automatic_seconds, "automatic"],
    ]
      .filter(([seconds]) => seconds > 0)
      .map(([seconds, kind]) => `${this._shortDuration(seconds)} ${kind}`);
    const breakdown = parts.length ? ` (incl. ${parts.join(", ")})` : "";
    return `${this._shortDuration(missed)}${breakdown}`;
  },

  // 130 → "2m 10s", 60 → "1m", 3720 → "1h 2m"; under a second → "<1s".
  _shortDuration(seconds) {
    const total = Math.round(Number(seconds) || 0);
    if (seconds > 0 && total < 1) return "<1s";
    const [big, small, units] =
      total < 3600
        ? [Math.floor(total / 60), total % 60, ["m", "s"]]
        : [
            Math.floor(total / 3600),
            Math.floor((total % 3600) / 60),
            ["h", "m"],
          ];
    if (!big) return `${small}s`;
    return `${big}${units[0]}${small ? ` ${small}${units[1]}` : ""}`;
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
