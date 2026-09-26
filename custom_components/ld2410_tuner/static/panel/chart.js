import { CHART_RANGES, GATE_COLORS } from "./constants.js";

export const panelChart = {
  _defaultKindAndGate(d) {
    const top = d.auto_learning?.last?.top_gates?.find((gate) =>
      /^g[0-8]_still$/.test(gate.key),
    );
    return { gate: top ? Number(top.key[1]) : 0, kind: "still" };
  },

  _chartHtml(id, d) {
    let cs = this._chartState.get(id);
    if (!cs) {
      cs = { ...this._defaultKindAndGate(d), hours: 6 };
      this._chartState.set(id, cs);
    }
    const kindOptions = ["still", "move"]
      .map(
        (k) =>
          `<option value="${k}" ${k === cs.kind ? "selected" : ""}>${k === "move" ? "Movement" : "Still"}</option>`,
      )
      .join("");
    const rangeOptions = CHART_RANGES.map(
      (r) =>
        `<option value="${r.hours}" ${r.hours === cs.hours ? "selected" : ""}>${this._esc(r.label)}</option>`,
    ).join("");
    const gateChips = Array.from({ length: 9 }, (_, g) => g)
      .map(
        (g) =>
          `<button type="button" class="gate-chip${g === cs.gate ? " active" : ""}" data-action="chart-pick-gate" data-gate="${g}" aria-label="Highlight gate ${g}" aria-pressed="${g === cs.gate}" style="--chip-color:${GATE_COLORS[g]}">G${g}</button>`,
      )
      .join("");
    return `<div class="chart-controls">
        <label>Kind<br><select data-action="chart-kind">${kindOptions}</select></label>
        <label>Range<br><select data-action="chart-range">${rangeOptions}</select></label>
        <button data-action="chart-refresh" type="button">Refresh</button>
      </div>
      <div class="gate-chips">${gateChips}</div>
      <div class="chart-status muted" role="status" data-chart-status="${id}"></div>
      <div class="chart-canvas" data-chart-canvas="${id}"><div class="muted">Loading…</div></div>
      <div class="chart-legend">
        <span><i class="swatch present"></i>Present (labelled)</span>
        <span><i class="swatch not_present"></i>Not present (labelled)</span>
        <span><i class="swatch unknown"></i>Unknown (labelled)</span>
        <span><i class="lineswatch current"></i>Current threshold</span>
        <span><i class="lineswatch learned"></i>Learned threshold</span>
        <span><i class="lineswatch noise"></i>Highest not-present sample</span>
      </div>
      <div class="muted" style="margin-top:6px">All 9 gates of the selected kind are shown together, each in its own color (select a G0–G8 button to highlight its shaded band).</div>`;
  },

  _chartKeys(cs) {
    return Array.from({ length: 9 }, (_, g) => `g${g}_${cs.kind}`);
  },

  _maybeFetchChart(id, d) {
    const cs = this._chartState.get(id);
    if (!cs) return;
    this._renderChartCanvas(id);
    if (this._collapsed.has(id) || this._sectionCollapsed(id, "chart")) return;
    if (cs.loading || cs.error) return;
    const revision = d.history?.revision || 0;
    if (cs.loadedRevision !== revision) cs.cache?.clear();
    if (
      cs.data &&
      cs.loadedKind === cs.kind &&
      cs.loadedHours === cs.hours &&
      cs.loadedRevision === revision
    )
      return;
    this._fetchChartData(id);
  },

  _queueChartCall(call) {
    return new Promise((resolve, reject) => {
      this._chartQueue.push({ call, resolve, reject });
      this._drainChartQueue();
    });
  },

  _drainChartQueue() {
    while (this._activeCharts < 2 && this._chartQueue.length) {
      const { call, resolve, reject } = this._chartQueue.shift();
      this._activeCharts++;
      Promise.resolve()
        .then(call)
        .then(resolve, reject)
        .finally(() => {
          this._activeCharts--;
          this._drainChartQueue();
        });
    }
  },

  async _fetchChartData(id, refresh = false) {
    const cs = this._chartState.get(id);
    if (!cs) return;
    if (refresh) {
      if (!cs.historyLinked) cs.end = null;
      cs.cache?.clear();
      if (!cs.historyLinked) cs.selection = null;
      cs.panelOpen = false;
    }
    const { kind, hours } = cs;
    const end = cs.end;
    const revision = this._data?.devices?.[id]?.history?.revision || 0;
    const key = JSON.stringify([kind, hours, end, revision]);
    if (cs.loading && cs.pendingKey === key) return;
    const request = (cs.request || 0) + 1;
    cs.request = request;
    cs.pendingKey = key;
    cs.cache ||= new Map();
    const cached = cs.cache.get(key);
    if (cached) {
      cs.data = cached;
      cs.loadedKind = kind;
      cs.loadedHours = hours;
      cs.loadedRevision = revision;
      cs.error = null;
      cs.loading = false;
      this._renderChartCanvas(id);
      return;
    }
    cs.loading = true;
    cs.error = null;
    this._renderChartCanvas(id);
    try {
      const data = await this._queueChartCall(() => {
        if (request !== cs.request || !this.isConnected) return null;
        return this._call(
          "history_series_multi",
          {
            device_id: id,
            keys: this._chartKeys({ kind }),
            hours,
            ...(end != null ? { end } : {}),
          },
          15000,
        );
      });
      if (request !== cs.request || !data) return;
      cs.data = data;
      cs.end = data.end;
      cs.loadedKind = kind;
      cs.loadedHours = hours;
      cs.loadedRevision = revision;
      cs.loadedAt = Date.now();
      cs.error = null;
      cs.cache.set(JSON.stringify([kind, hours, cs.end, revision]), data);
      while (cs.cache.size > 6) cs.cache.delete(cs.cache.keys().next().value);
    } catch (err) {
      if (request === cs.request) cs.error = err?.message || String(err);
    } finally {
      if (request === cs.request) {
        cs.loading = false;
        // A focused range/kind selector must not prevent a completed chart
        // from rendering. Only an active drag or chart text edit defers it.
        this._renderChartCanvas(id);
      }
    }
  },

  _renderChartCanvas(id, force = false) {
    const el = this.shadowRoot?.querySelector(
      `[data-chart-canvas="${CSS.escape(id)}"]`,
    );
    if (!el) return;
    if (this._busyCards.has(id)) {
      this._redrawPending = true;
      return;
    }
    const cs = this._chartState.get(id);
    const d = this._data?.devices?.[id];
    if (!cs || !d) {
      el.innerHTML = `<div class="muted">Unavailable</div>`;
      return;
    }
    const active = this.shadowRoot.activeElement;
    if (
      this._dragging ||
      (cs.panelOpen && el.contains(active) && active?.tagName === "INPUT")
    )
      return;
    const card = el.closest("[data-device-id]");
    this._syncChartControls(card, cs);
    const activeKey = `g${cs.gate}_${cs.kind}`;
    const signature = JSON.stringify([
      cs.gate,
      cs.kind,
      cs.hours,
      cs.historyLinked,
      cs.loadedKind,
      cs.loadedHours,
      cs.loading,
      cs.error,
      cs.selection,
      cs.panelOpen,
      d.current_thresholds?.[activeKey],
      d.last_learning?.proposals,
      d.last_learning?.training,
      d.last_learning?.status,
    ]);
    if (
      !force &&
      el._renderedData === cs.data &&
      el._renderedState === signature
    )
      return;
    el._renderedData = cs.data;
    el._renderedState = signature;
    const matches =
      cs.data &&
      cs.loadedKind === cs.kind &&
      cs.loadedHours === cs.hours &&
      (cs.end == null || cs.data.end === cs.end);
    const status = this.shadowRoot.querySelector(
      `[data-chart-status="${CSS.escape(id)}"]`,
    );
    if (status) {
      status.textContent = this._chartStatusText(cs, matches);
      status.classList.toggle("is-busy", !!cs.loading);
    }
    const refresh = card.querySelector('[data-action="chart-refresh"]');
    refresh.disabled = !!cs.loading;
    refresh.setAttribute("aria-busy", String(!!cs.loading));
    el.setAttribute("aria-busy", String(!!cs.loading));
    this._renderChartData(id, el, d, cs, matches);
  },

  _renderChartData(id, el, d, cs, matches) {
    if (!matches) {
      el.innerHTML = `<div class="muted">${cs.error ? "History unavailable. Use Refresh to retry." : "Loading history…"}</div>`;
      return;
    }
    const anyPoints = Object.values(cs.data.series || {}).some(
      (series) => series.points?.length,
    );
    if (!anyPoints) {
      el.innerHTML = `<div class="muted">No recorded history in this window.</div>`;
      return;
    }
    try {
      el.innerHTML = this._buildChartSvg(d, cs);
      this._wireChartSelection(id, el, d, cs);
      const review = el.querySelector('[data-action="review-results"]');
      if (review)
        review.onclick = () => {
          this._setSectionCollapsed(id, "details", false);
          const section = el
            .closest(".card")
            .querySelector('.subsection[data-section="details"]');
          section.classList.remove("collapsed");
          section.querySelector(".sub-toggle").textContent = "▾";
          section
            .querySelector(".sub-toggle")
            .setAttribute("aria-expanded", "true");
          section.scrollIntoView({ behavior: "smooth", block: "nearest" });
        };
    } catch (err) {
      el.innerHTML = `<div class="muted">Unable to render chart: ${this._esc(err?.message || err)}</div>`;
    }
  },

  _falsePositiveSourcesHtml(learning) {
    if (learning?.status !== "unsafe") return "";
    const sources = Object.entries(learning.proposals || {})
      .filter(
        ([key, proposal]) =>
          /^g[0-8]_(move|still)$/.test(key) && proposal.false_positives > 0,
      )
      .sort((a, b) => b[1].false_positives - a[1].false_positives)
      .slice(0, 3)
      .map(([key, proposal]) => {
        const [, gate, kind] = /^g([0-8])_(move|still)$/.exec(key);
        return `Gate ${gate} ${kind === "move" ? "Movement" : "Still"} at ${Math.round(proposal.threshold)}: ${proposal.false_positives} / ${proposal.not_present_samples} empty-room samples.${this._presenceDependencyText(proposal)}`;
      });
    return sources.length
      ? `<div class="false-positive-sources">Largest per-gate false-positive counts: ${this._esc(sources.join("; "))}. Gates can trigger on the same samples; these counts must not be added.</div>`
      : "";
  },

  _learnStatusHtml(proposal, cs, learning) {
    const label = `Gate ${cs.gate} · ${cs.kind === "move" ? "Movement" : "Still"}`;
    if (!proposal)
      return `<div class="learn-status muted">${this._esc(label)}: click "Learn thresholds" to compute a recommendation.</div>`;
    const noiseNote = this._noiseNote(proposal);
    if (proposal.status === "provisional")
      return `<div class="learn-status warn">${this._esc(label)}: saved threshold ${Math.round(proposal.threshold)} was produced by the previous learner. Learn again with the current model before Apply.</div>`;
    if (proposal.threshold === 100) {
      const measured = learning?.training;
      const coverage = measured?.present_samples
        ? ` The combined gates detect ${measured.present_samples - measured.false_negatives} / ${measured.present_samples} human-labelled presence samples; ${measured.false_negatives} are missed by every gate.`
        : " No human-labelled device presence measurement is available.";
      const state =
        learning?.status === "unsafe"
          ? "Combined device targets not met; manual Apply is available."
          : "Review the combined device results before Apply.";
      return `<div class="learn-status ${learning?.status === "unsafe" ? "warn" : "muted"}">${this._esc(label)}: threshold 100 disables this gate; presence detection relies on the other enabled gates.${noiseNote}${this._esc(coverage)} ${state}<button type="button" data-action="review-results">Review device results</button></div>`;
    }
    if (proposal.status === "ok")
      return this._acceptedStatusHtml(proposal, label, noiseNote);
    if (proposal.status === "unsafe") {
      const gateResult =
        proposal.not_present_samples > 0 && proposal.false_positives != null
          ? ` This gate alone triggers on ${proposal.false_positives} / ${proposal.not_present_samples} human-labelled empty-room samples.`
          : " No human-labelled empty-room measurement is available for this gate.";
      return `<div class="learn-status warn"><div>${this._esc(label)}: candidate threshold ${Math.round(proposal.threshold)}.${this._esc(gateResult)}${noiseNote}${this._esc(this._presenceDependencyText(proposal))}</div><div><b>Combined device recommendation unsafe</b> — ${this._esc(proposal.message || "The combined thresholds did not meet the learning targets")}. These failure counts cover all enabled gates, not just this gate.</div><button type="button" data-action="review-results">Review device results</button><div>Candidate shown as a red dashed line; manual Apply is available despite these results.</div></div>`;
    }
    return `<div class="learn-status warn">${this._esc(label)}: not enough data yet — ${this._esc(proposal.message || "need more present and not-present samples")}.</div>`;
  },

  _syncChartControls(card, cs) {
    for (const [action, value] of [
      ["chart-kind", cs.kind],
      ["chart-range", cs.hours],
    ]) {
      const control = card.querySelector(`[data-action="${action}"]`);
      if (control && action === "chart-range") {
        for (const option of [...control.options])
          if (option.dataset.customRange && option.value !== String(value))
            option.remove();
        if (
          ![...control.options].some((option) => option.value === String(value))
        ) {
          const option = new Option("Selected period", String(value));
          option.dataset.customRange = "true";
          control.add(option);
        }
      }
      if (control && control.value !== String(value))
        control.value = String(value);
    }
    this._updateGateChipHighlight(card, cs.gate);
  },

  _chartStatusText(cs, matches) {
    if (cs.error) {
      const previous = matches ? " Showing the previous window." : "";
      return `Couldn't refresh history: ${cs.error}${previous}`;
    }
    const loading = cs.loading ? "Loading history… " : "";
    if (!matches) return loading;
    return `${loading}Window ends ${new Date(cs.data.end * 1000).toLocaleString()}. ${cs.data.bucket_seconds || "—"}s buckets: mean line, sampled min–max band. ${cs.historyLinked ? "Refresh keeps this historical window." : "Refresh moves to latest."}`;
  },

  _noiseNote(proposal) {
    if (proposal.noise_ceiling == null) return "";
    const source =
      proposal.noise_source === "automatic" ? "estimated" : "labelled";
    const separation = proposal.separation;
    const margin = separation?.separated
      ? ` Retained presence reference (lower quartile): ${Math.round(separation.presence_reference)}; preferred threshold between noise and presence: ${Math.round(separation.preferred_threshold)}.`
      : "";
    return ` Highest ${source} NOT PRESENT sample seen: ${Math.round(proposal.noise_ceiling)} (99th percentile: ${Math.round(proposal.noise_floor_p99)}).${margin}`;
  },

  _acceptedStatusHtml(proposal, label, noiseNote) {
    if (proposal.evidence_basis === "automatic")
      return `<div class="learn-status muted">${this._esc(label)}: estimated threshold ${Math.round(proposal.threshold)} from confidence-weighted observations. No human-labelled accuracy measurement yet.${noiseNote}</div>`;
    if (proposal.role === "unchanged")
      return `<div class="learn-status muted">${this._esc(label)}: no usable observations for this gate; its current threshold is preserved.</div>`;

    return `<div class="learn-status ok">${this._esc(label)}: learned threshold ${Math.round(proposal.threshold)} — this gate detects ${Math.round((proposal.sensitivity || 0) * 100)}% of human-labelled training samples, ${proposal.false_positives || 0} exception(s) out of ${proposal.not_present_samples} not-present samples.${noiseNote}</div>`;
  },
};
