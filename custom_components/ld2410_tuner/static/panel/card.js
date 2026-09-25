export const panelCards = {
  _createCard(id, d, previousChart) {
    if (!this._seenDevices.has(id)) {
      this._seenDevices.add(id);
      this._collapsed.add(id);
    }
    const card = document.createElement("div");
    card.className = "card";
    card.dataset.deviceId = id;
    const info = this._autoInfo(d.auto_learning || {});
    const isCollapsed = this._collapsed.has(id);
    card.innerHTML =
      this._cardHeaderHtml(d, info, isCollapsed) +
      `<div class="action-status muted" role="status" aria-live="polite"></div>` +
      `<div class="body${isCollapsed ? " collapsed" : ""}">${this._cardBodyHtml(id, d, info)}</div>`;
    if (previousChart)
      card.querySelector('[data-section="chart"]').replaceWith(previousChart);
    card.querySelector(".gate-results").open = !!this._sectionState.get(
      `${id}:gate-table`,
    );
    this._wireCard(card, id, d);
    return card;
  },

  _cardHeaderHtml(d, info, isCollapsed) {
    const { autoState, autoPill, canFeedback, al } = info;
    const confidence = canFeedback
      ? " · " + Math.round((al.confidence || 0) * 100) + "%"
      : "";
    return `<div class="top" data-action="toggle-top"><div><div class="name">${this._esc(d.name)}<span class="pill ${autoState}" title="Latest automatic reading">${autoPill}${confidence}</span></div><div class="area">${this._esc(d.area_id || "No area")}</div></div><button class="toggle" data-action="toggle" aria-label="${isCollapsed ? "Expand" : "Collapse"}" aria-expanded="${!isCollapsed}">${isCollapsed ? "▸" : "▾"}</button></div>`;
  },

  _cardBodyHtml(id, d, info) {
    return [
      this._section(
        id,
        "training",
        "Current presence label",
        this._trainingHtml(d),
      ),
      this._section(id, "chart", "Gate visualization", this._chartHtml(id, d)),
      this._section(
        id,
        "history",
        "Past presence labels",
        this._historyHtml(id, d),
      ),
      this._section(
        id,
        "auto",
        "Automatic analysis & feedback",
        this._autoHtml(d.auto_learning || {}, info),
      ),
      this._section(
        id,
        "details",
        "Recommendations",
        this._recommendationsHtml(d),
      ),
      this._section(id, "actions", "Data & export", this._dataActionsHtml()),
    ].join("");
  },

  _trainingHtml(d) {
    const state = d.training_state || "unknown";
    const expiry = d.training_expires_at
      ? `<span class="area">Expires ${this._timeLeft(d.training_expires_at)}</span>`
      : "";
    return `<div class="training"><label>Training state</label><select data-action="state"><option value="unknown" ${state === "unknown" ? "selected" : ""}>UNKNOWN</option><option value="present" ${state === "present" ? "selected" : ""}>PRESENT</option><option value="not_present" ${state === "not_present" ? "selected" : ""}>NOT PRESENT</option></select><label>Timeout</label><div class="duration"><input data-action="timeout-hours" type="number" min="0" step="1" inputmode="numeric" placeholder="Hours"><span>h</span><input data-action="timeout-minutes" type="number" min="0" max="59" step="1" inputmode="numeric" placeholder="Minutes"><span>m</span></div>${expiry}</div>`;
  },

  _autoInfo(auto) {
    const al = auto.last;
    const autoState = al?.state || "unknown";
    let autoPill = "AUTO NOT PRESENT";
    if (autoState === "unknown") autoPill = "UNKNOWN";
    else if (autoState === "present") autoPill = "AUTO PRESENT";
    const canFeedback = autoState === "present" || autoState === "not_present";
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
        .join(" · ") || "No confident classification yet";
    const confidence = al ? Math.round((al.confidence || 0) * 100) + "%" : "—";
    const calibNote = this._calibrationNote(auto.calibration);
    return `<div class="auto-head"><span class="pill ${autoState}">${autoPill}</span><span class="muted">Training weight: 20% × confidence; human labels take priority</span></div><div class="auto-details"><div><span>Estimated confidence</span>${confidence}</div><div><span>Evidence source</span>${al?.basis === "human-guided" ? "Labelled examples" : "Background estimate"}</div><div><span>Segments</span>${auto.segments || 0}</div></div><div class="muted" style="margin-top:7px">${this._esc(top)}</div><div class="muted">${auto.observations || 0} estimates stored. Confidence is an estimate, not measured accuracy.</div><div class="feedback-row"><span class="muted">Was this reading right?</span><button class="fb-btn correct" data-action="auto-feedback" data-correct="true" ${canFeedback ? "" : "disabled"}>✓ Correct</button><button class="fb-btn incorrect" data-action="auto-feedback" data-correct="false" ${canFeedback ? "" : "disabled"}>✗ Incorrect</button></div><div class="muted" style="margin-top:5px">Feedback accuracy — present: ${this._feedbackText(auto.feedback, "present")}, not present: ${this._feedbackText(auto.feedback, "not_present")}${calibNote}</div>`;
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
