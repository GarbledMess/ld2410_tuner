export const panelCards = {
  _createCard(id, d, previousCard, placement, options = {}) {
    d = this._learningView(id, d);
    if (!this._seenDevices.has(id)) {
      this._seenDevices.add(id);
      this._collapsed.add(id);
    }
    let card = document.createElement("div");
    card.className = "card";
    card.dataset.deviceId = id;
    card.dataset.placement = placement;
    const info = this._autoInfo(d.auto_learning || {});
    const isCollapsed = this._cardCollapsed(placement);
    const lone = this._loneInGroup(id, options.groupId)
      ? options.groupId
      : null;
    const bodyId = `device-body-${Array.from(placement, (char) => char.codePointAt(0).toString(16)).join("-")}`;
    card.innerHTML =
      this._cardHeaderHtml(id, d, info, isCollapsed, options) +
      this._recordingHtml(d) +
      `<div class="action-status muted" role="status" aria-live="polite"></div>` +
      `<div class="learning-job-status" role="status" aria-live="polite">${this._learningJobHtml(d)}</div>` +
      `<button type="button" class="toggle" data-action="toggle" aria-controls="${bodyId}" aria-expanded="${!isCollapsed}">${isCollapsed ? "Show" : "Hide"} device details</button>` +
      `<div id="${bodyId}" class="body${isCollapsed ? " collapsed" : ""}">${this._cardBodyHtml(id, d, info, lone)}</div>`;
    for (const section of card.querySelectorAll(".subsection")) {
      const content = section.querySelector(".subsection-body");
      content.id = `${bodyId}-${section.dataset.section}`;
      section
        .querySelector(".subsection-head")
        .setAttribute("aria-controls", content.id);
    }
    card.querySelector(".gate-results").open = !!this._sectionState.get(
      `${id}:gate-table`,
    );
    for (const detail of card.querySelectorAll("details[data-detail]"))
      detail.open = !!this._sectionState.get(
        `${id}:detail-${detail.dataset.detail}`,
      );
    this._placeHistoryGraph(card, id);
    if (previousCard) {
      this._patchElement(previousCard, card);
      card = previousCard;
    }
    this._wireCard(card, id, d);
    requestAnimationFrame(() => this._syncHistoryTimeline(card, id));
    return card;
  },

  _cardHeaderHtml(id, d, info, isCollapsed, options = {}) {
    const { autoState, autoPill, al } = info;
    const group = this._memberGroup(id, options.groupId);
    const confidence =
      autoState !== "unknown"
        ? " · " + Math.round((al.confidence || 0) * 100) + "%"
        : "";
    return `<div class="top"><div><div class="name">${this._esc(d.name)}<span class="pill ${autoState}" title="Latest automatic reading">${autoPill}${confidence}</span>${this._nightlyMarker(d)}${options.notRecording ? '<span class="pill not-recording">Not recording</span>' : ""}</div><div class="area">${group ? "" : this._esc(this._roomsData().areas[d.area_id] || d.area_id || "No area")}</div>${group ? this._roomRoleHtml(id, group) : this._comparisonHeaderHtml(d)}<span class="section-description">Live presence, recorded history and tuning recommendations.</span></div></div>${group ? this._ownScoreHtml(d) : ""}`;
  },

  // A radar alone in its room or zone is labelled on that group's timeline, which
  // trains it (rooms/lone_labels.py); its own editor opens only when asked for.
  _loneInGroup(id, groupId) {
    const members = this._recordingMembers(this._roomsData().groups[groupId]);
    return (
      members.length === 1 &&
      members[0] === id &&
      !this._radarEditorShown.has(id)
    );
  },

  // Like areas, a group counts only its recording radars (rooms/lone_labels.py).
  _recordingMembers(group) {
    const members = this._roomsData().members;
    return (group?.device_ids || []).filter(
      (id) => members[id]?.recording_enabled !== false,
    );
  },

  _loneHistoryHtml(groupId) {
    const name = this._roomsData().groups[groupId]?.name || "its room";
    return `<p class="field-note">Label this radar on ${this._esc(name)}'s timeline above.</p>`;
  },

  _cardBodyHtml(id, d, info, lone = null) {
    return [
      this._section(
        id,
        "training",
        "Current presence label",
        this._trainingHtml(id, d) +
          (this._radarSourcesShown(id, d) ? this._sourcesHtml(id, d) : ""),
      ),
      `<div class="chart-home">${this._section(id, "chart", "Gate visualization", this._chartHtml(id, d))}</div>`,
      this._section(
        id,
        "history",
        "History",
        lone ? this._loneHistoryHtml(lone) : this._historyHtml(id, d),
      ),
      this._section(
        id,
        "auto",
        "Automatic analysis & feedback",
        this._autoHtml(d.auto_learning || {}, {
          ...info,
          refreshing: d.references_refreshing,
        }),
      ),
      this._section(
        id,
        "details",
        "Recommendations",
        this._recommendationsHtml(d, id),
      ),
      this._section(id, "recorded", "Recorded data", this._recordedHtml(id)),
      this._section(id, "actions", "Data & export", this._dataActionsHtml()),
    ].join("");
  },

  _trainingHtml(id, d) {
    const state = d.training_state || "unknown";
    const reasonId = `presence-reason-${Array.from(id, (char) => char.codePointAt(0).toString(16)).join("-")}`;
    const paused = d.recording_enabled === false;
    const lock = paused ? `disabled aria-describedby="${reasonId}"` : "";
    const reason = paused
      ? `<p class="field-note" id="${reasonId}">Recording is off for this radar, so presence can't be labelled. Turn on Record data above to label it.</p>`
      : "";
    const expiry = d.training_expires_at
      ? `<span class="area">Expires ${this._timeLeft(d.training_expires_at)}</span>`
      : "";
    return `<div class="training"><label>Presence now</label><select data-action="state" ${lock}><option value="unknown" ${state === "unknown" ? "selected" : ""}>UNKNOWN</option><option value="present" ${state === "present" ? "selected" : ""}>PRESENT</option><option value="not_present" ${state === "not_present" ? "selected" : ""}>NOT PRESENT</option></select><label>Keep label for</label><div class="duration"><input data-action="timeout-hours" ${lock} aria-label="Label duration in hours" type="number" min="0" step="1" inputmode="numeric" placeholder="Hours"><span>h</span><input data-action="timeout-minutes" ${lock} aria-label="Label duration in minutes" type="number" min="0" max="59" step="1" inputmode="numeric" placeholder="Minutes"><span>m</span></div>${expiry}${reason}</div>`;
  },

  _autoInfo(auto) {
    const al = auto.last;
    const autoState = al?.state || "unknown";
    let autoPill = "AUTO NOT PRESENT";
    if (autoState === "unknown") autoPill = "UNKNOWN";
    else if (autoState === "present") autoPill = "AUTO PRESENT";
    if (al?.basis === "external" && autoState !== "unknown")
      autoPill = `SOURCE ${autoState === "present" ? "PRESENT" : "NOT PRESENT"}`;
    const canFeedback =
      al?.basis !== "external" &&
      (autoState === "present" || autoState === "not_present");
    return { al, autoState, autoPill, canFeedback };
  },

  _feedbackText(feedback, key) {
    const value = feedback?.[key];
    return value?.total ? `${value.correct}/${value.total}` : "—";
  },

  _calibrationNote(calibration) {
    const calib = calibration || {};
    const calibBits = [];
    if (calib.present_bias)
      calibBits.push(
        `present threshold +${Number(calib.present_bias).toFixed(2)}`,
      );
    if (calib.absent_bias)
      calibBits.push(
        `not present threshold +${Number(calib.absent_bias).toFixed(2)}`,
      );
    return calibBits.length
      ? ` · Calibration from feedback: ${calibBits.join(", ")}`
      : "";
  },

  _autoHtml(auto, info) {
    const { al, autoState, autoPill, canFeedback } = info;
    const top =
      al?.top_gates
        ?.slice(0, 3)
        .map((gate) => `${this._esc(gate.key)} ${this._fmt(gate.energy)}`)
        .join(" · ") ||
      (al?.basis === "external"
        ? (al.sources || []).join(" · ")
        : "No confident classification yet");
    const confidence = al ? Math.round((al.confidence || 0) * 100) + "%" : "—";
    const calibNote = this._calibrationNote(auto.calibration);
    const source =
      {
        external: "External entities (configured confidence)",
        "adaptive-guided": "Age-weighted room references",
        "human-guided": "Labelled examples",
      }[al?.basis] || "Background estimate";
    return `<div class="auto-head"><span class="pill ${autoState}">${autoPill}</span><span class="muted">Confidence-weighted time; human labels take priority</span></div><div class="auto-details"><div><span>Estimated confidence</span>${confidence}</div><div><span>Evidence source</span>${source}</div><div><span>Segments</span>${auto.segments || 0}</div></div><div class="muted" style="margin-top:7px">${this._esc(top)}</div><div class="muted">${auto.observations || 0} estimates stored. Confidence is an estimate, not measured accuracy.</div>${this._referenceHtml(auto, info.refreshing)}<div class="feedback-row"><span class="muted">Was this reading right?</span><button class="fb-btn correct" data-action="auto-feedback" data-correct="true" ${canFeedback ? "" : "disabled"}>✓ Correct</button><button class="fb-btn incorrect" data-action="auto-feedback" data-correct="false" ${canFeedback ? "" : "disabled"}>✗ Incorrect</button></div><div class="muted" style="margin-top:5px">Feedback accuracy — present: ${this._feedbackText(auto.feedback, "present")}, not present: ${this._feedbackText(auto.feedback, "not_present")}${calibNote}</div>`;
  },

  _referenceHtml(auto, refreshing) {
    const profile = auto.profile;
    if (!profile) return "";
    const counts = profile.periods || {};
    const novelty = auto.last?.unfamiliar_gates || [];
    const warning = novelty.length
      ? `<div class="muted reference-review" role="status">Unfamiliar readings at ${novelty.map((key) => this._esc(key)).join(", ")}. Confidence is reduced. Mark a known period or use an independent entity to confirm the room’s current behaviour.</div>`
      : "";
    const stale = refreshing
      ? `<div class="muted reference-refreshing" role="status">Refreshing references…</div>`
      : auto.stale_since
        ? `<div class="muted reference-stale" role="status">References out of date since ${this._esc(new Date(auto.stale_since * 1000).toLocaleString())}. They rebuild daily at the learning schedule time, even with overnight learning off, or now with Refresh references.</div>`
        : "";
    return `<div class="muted reference-status">Room references: ${counts.human || 0} human, ${counts.entity || 0} entity and ${counts.radar || 0} inferred periods. Recent corrections have more influence; repeated guesses do not increase independent confidence. Human labels and configured entities continue teaching this model.</div>${stale}<div class="reference-actions"><button type="button" data-action="refresh-references" ${refreshing ? "disabled" : ""}>Refresh references</button></div>${warning}`;
  },

  _gateTables(d) {
    const counts = d.sample_counts || {};
    const rows = [],
      mobile = [];
    for (let g = 0; g < 9; g++) {
      const keys = [`g${g}_move`, `g${g}_still`],
        mc = counts[keys[0]] || {},
        sc = counts[keys[1]] || {},
        mp = d.last_learning?.proposals?.[keys[0]],
        sp = d.last_learning?.proposals?.[keys[1]];
      rows.push(
        `<tr><td>G${g}</td><td>${this._fmt(d.current_thresholds?.[keys[0]])}</td><td class="${mp?.status === "ok" ? "learned" : ""}">${this._fmt(mp?.threshold)}</td><td>${mc.present || 0}/${mc.not_present || 0}</td><td>${this._fmt(d.current_thresholds?.[keys[1]])}</td><td class="${sp?.status === "ok" ? "learned" : ""}">${this._fmt(sp?.threshold)}</td><td>${sc.present || 0}/${sc.not_present || 0}</td></tr>`,
      );
      mobile.push(
        `<div class="gate"><div class="gate-head"><span>Gate ${g}</span><span>Move / Still</span></div><div class="gate-grid"><div><span>Current</span>${this._fmt(d.current_thresholds?.[keys[0]])} / ${this._fmt(d.current_thresholds?.[keys[1]])}</div><div><span>Learned</span><b class="${mp?.status === "ok" || sp?.status === "ok" ? "learned" : ""}">${this._fmt(mp?.threshold)} / ${this._fmt(sp?.threshold)}</b></div><div><span>Move P/N</span>${mc.present || 0} / ${mc.not_present || 0}</div><div><span>Still P/N</span>${sc.present || 0} / ${sc.not_present || 0}</div></div></div>`,
      );
    }
    return { rows, mobile };
  },

  _detailsHtml(d) {
    const counts = Object.values(d.sample_counts || {});
    const totalPresent = counts.reduce(
      (total, value) => total + (value.present || 0),
      0,
    );
    const totalAbsent = counts.reduce(
      (total, value) => total + (value.not_present || 0),
      0,
    );
    const { rows, mobile } = this._gateTables(d);
    return `<div class="stats"><div class="stat">Human gate samples · present<b>${totalPresent}</b></div><div class="stat">Human gate samples · absent<b>${totalAbsent}</b></div></div><div class="table-scroll"><table><thead><tr><th>Gate</th><th>Move now</th><th>Move learned</th><th>Move P/N</th><th>Still now</th><th>Still learned</th><th>Still P/N</th></tr></thead><tbody>${rows.join("")}</tbody></table></div><div class="mobile-gates">${mobile.join("")}</div>`;
  },
};
