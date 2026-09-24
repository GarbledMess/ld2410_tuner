const CHART_W = 760, CHART_H = 220;
const CHART_MARGIN = { left: 34, right: 10, top: 10, bottom: 24 };
const CHART_PLOT_W = CHART_W - CHART_MARGIN.left - CHART_MARGIN.right;
const CHART_PLOT_H = CHART_H - CHART_MARGIN.top - CHART_MARGIN.bottom;

const CHART_RANGES = [
  { label: "1 hour", hours: 1 },
  { label: "6 hours", hours: 6 },
  { label: "24 hours", hours: 24 },
  { label: "3 days", hours: 72 },
  { label: "7 days", hours: 168 },
  { label: "30 days", hours: 720 },
];

// One distinct color per gate (0-8), shared between "move" and "still" views
// so a gate keeps its identity when you switch kind.
const GATE_COLORS = ["#e53935","#fb8c00","#c0a000","#43a047","#00897b","#1e88e5","#5e35b1","#8e24aa","#6d4c41"];

// Which card subsections start collapsed the first time a device is drawn.
// Training + the chart are the two things you actually need to glance at
// while tuning, so those two start open; everything else is one tap away.
const SECTION_DEFAULTS = { training: false, chart: false, history: true, auto: true, details: true, actions: true };

class LD2410TunerPanel extends HTMLElement {
  constructor() {
    super();
    // Which device cards are collapsed, keyed by device id. Kept on the
    // instance (not recomputed in _draw) so a collapse/expand survives the
    // periodic redraw instead of springing back open every poll.
    this._collapsed = new Set();
    // Devices we've already applied the "start collapsed" default to, so a
    // card the user deliberately expanded doesn't get re-collapsed under them.
    this._seenDevices = new Set();
    // `${deviceId}:${sectionKey}` -> collapsed boolean, for the subsections
    // nested inside an expanded card.
    this._sectionState = new Map();
    // Per-device chart UI state: selected gate/range, last fetched series,
    // loading/error flags, and any in-progress drag value.
    this._chartState = new Map();
    this._dragging = false;
    this._drafts = new Map();
    this._chartQueue = [];
    this._activeCharts = 0;
    this._onVisibilityChange = () => { if (!document.hidden) this._load(); };
  }

  set hass(hass) {
    this._hass = hass;
    if (!this.shadowRoot) this._render();
    if (!this._loaded) this._load();
    if (!this._pollTimer) {
      // Skip polling while the tab/panel isn't visible - no point hitting
      // the websocket every 3s for a view nobody's looking at - and catch
      // up immediately when it becomes visible again.
      this._pollTimer = setInterval(() => { if (!document.hidden) this._load(); }, 3000);
      document.addEventListener("visibilitychange", this._onVisibilityChange);
    }
  }

  connectedCallback() {
    if (this._hass) this.hass = this._hass;
  }

  disconnectedCallback() {
    this._endDrag?.();
    if (this._pollTimer) clearInterval(this._pollTimer);
    this._pollTimer = null;
    document.removeEventListener("visibilitychange", this._onVisibilityChange);
  }

  // True while focus sits in a field the person is actively using (typing a
  // number, a native date/time picker that's open, or dragging a threshold
  // handle on a chart). Rebuilding the DOM underneath any of those is what
  // was closing date pickers, dropping keystrokes, and would just as easily
  // yank a chart's drag handle back mid-gesture, so a redraw during this
  // window is deferred rather than applied immediately.
  _isEditing() {
    if (this._dragging) return true;
    const active = this.shadowRoot?.activeElement;
    if (!active) return false;
    return ["INPUT", "SELECT", "TEXTAREA"].includes(active.tagName);
  }

  _render() {
    this.attachShadow({mode: "open"});
    this.shadowRoot.innerHTML = `
      <style>
        :host { display:block; padding:12px; box-sizing:border-box; color:var(--primary-text-color); }
        .wrap { max-width:1500px; margin:auto; }
        h1 { font-size:26px; margin:4px 0 6px; }
        .subtitle { color:var(--secondary-text-color); font-size:14px; margin-bottom:14px; }
        .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(500px,1fr)); gap:14px; }
        .card { min-width:0; align-self:start; background:var(--ha-card-background,var(--card-background-color,#fff)); border-radius:12px; padding:16px; box-shadow:var(--ha-card-box-shadow,0 2px 8px rgba(0,0,0,.12)); }
        .top { display:flex; justify-content:space-between; gap:12px; align-items:flex-start; cursor:pointer; user-select:none; }
        .toggle { min-height:32px; padding:4px 10px; font-size:16px; line-height:1; flex-shrink:0; }
        .body.collapsed { display:none; }
        .name { font-size:20px; font-weight:600; display:flex; align-items:center; flex-wrap:wrap; gap:8px; }
        .area,.muted { color:var(--secondary-text-color); font-size:13px; }
        select,button { font:inherit; min-height:44px; padding:8px 12px; border-radius:9px; border:1px solid var(--divider-color); background:var(--card-background-color); color:var(--primary-text-color); box-sizing:border-box; }
        button { cursor:pointer; }
        button.primary { background:var(--primary-color); color:var(--text-primary-color,#fff); border:0; }
        button:disabled { opacity:.5; cursor:default; }
        .training,.controls,.export { display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
        .controls,.export { margin-bottom:8px; }
        .controls:last-child,.export:last-child { margin-bottom:0; }
        .training label { color:var(--secondary-text-color); font-size:13px; }
        .duration { display:flex; align-items:center; gap:4px; }
        .duration input { width:82px; min-height:44px; padding:8px; border:1px solid var(--divider-color); border-radius:9px; background:var(--card-background-color); color:var(--primary-text-color); box-sizing:border-box; }
        .section-title { font-weight:600; margin-bottom:5px; }
        .history-fields { display:grid; grid-template-columns:1fr 1fr 180px; gap:8px; margin:10px 0; }
        .history-fields label { color:var(--secondary-text-color); font-size:12px; }
        .history-fields input,.history-fields select { display:block; width:100%; margin-top:4px; min-height:44px; padding:8px; border:1px solid var(--divider-color); border-radius:9px; background:var(--card-background-color); color:var(--primary-text-color); box-sizing:border-box; }
        .label-list { margin-top:10px; font-size:12px; color:var(--secondary-text-color); max-height:130px; overflow:auto; }
        .stats { display:grid; grid-template-columns:repeat(3,1fr); gap:8px; margin-bottom:12px; }
        .stat { padding:10px; border-radius:9px; background:var(--secondary-background-color); }
        .stat b { display:block; font-size:17px; margin-top:2px; }
        .table-scroll { overflow-x:auto; -webkit-overflow-scrolling:touch; }
        table { width:100%; border-collapse:collapse; font-size:13px; min-width:650px; }
        th,td { padding:7px 5px; text-align:right; border-bottom:1px solid var(--divider-color); white-space:nowrap; }
        th:first-child,td:first-child { text-align:left; }
        .learned { font-weight:700; }
        .ok { color:var(--state-active-color,#2e7d32); }
        .warn { color:var(--error-color,#c62828); }
        .notice { padding:10px; background:var(--secondary-background-color); border-radius:8px; margin-top:10px; font-size:13px; }
        .auto-head { display:flex; justify-content:space-between; gap:8px; align-items:center; }
        .pill { padding:4px 8px; border-radius:999px; background:var(--secondary-background-color); font-size:12px; font-weight:600; }
        .pill.present { color:var(--state-active-color,#2e7d32); }
        .pill.not_present { color:var(--secondary-text-color); }
        .auto-details { display:grid; grid-template-columns:repeat(3,1fr); gap:7px; margin-top:8px; font-size:12px; }
        .auto-details div { background:var(--secondary-background-color); padding:7px; border-radius:7px; }
        .auto-details span { display:block; color:var(--secondary-text-color); font-size:11px; }
        .feedback-row { display:flex; flex-wrap:wrap; align-items:center; gap:8px; margin-top:9px; }
        .fb-btn { min-height:36px; padding:5px 10px; font-size:12px; }
        .fb-btn.correct:not(:disabled):hover { border-color:var(--state-active-color,#2e7d32); }
        .fb-btn.incorrect:not(:disabled):hover { border-color:var(--error-color,#c62828); }
        .mobile-gates { display:none; }
        .gate { border:1px solid var(--divider-color); border-radius:9px; padding:10px; margin:7px 0; }
        .gate-head { display:flex; justify-content:space-between; font-weight:600; margin-bottom:7px; }
        .gate-grid { display:grid; grid-template-columns:1fr 1fr; gap:7px; font-size:13px; }
        .gate-grid div { background:var(--secondary-background-color); padding:7px; border-radius:7px; }
        .gate-grid span { display:block; color:var(--secondary-text-color); font-size:11px; margin-bottom:2px; }
        .subsection { border:1px solid var(--divider-color); border-radius:9px; margin-top:10px; overflow:hidden; }
        .subsection:first-child { margin-top:0; }
        .subsection-head { display:flex; justify-content:space-between; align-items:center; gap:8px; padding:9px 12px; cursor:pointer; user-select:none; font-weight:600; font-size:13px; background:var(--secondary-background-color); }
        .subsection-body { padding:12px; }
        .subsection.collapsed .subsection-body { display:none; }
        .sub-toggle { min-height:28px; padding:2px 8px; font-size:14px; flex-shrink:0; }
        .chart-controls { display:flex; flex-wrap:wrap; gap:8px; align-items:flex-end; margin-bottom:8px; }
        .chart-controls select { min-width:120px; }
        .chart-status { height:3.2em; overflow:auto; margin:4px 0; }
        .chart-controls button { min-width:90px; }
        .gate-chips { display:flex; flex-wrap:wrap; gap:5px; margin-bottom:8px; }
        .gate-chip { min-height:30px; padding:3px 9px; font-size:12px; border-width:2px; border-color:var(--chip-color); color:var(--chip-color); background:transparent; font-weight:600; }
        .gate-chip.active { background:var(--chip-color); color:#fff; }
        .gate-line { fill:none; stroke-width:1; opacity:.4; }
        .value-band { opacity:.16; stroke:none; }
        .value-line { fill:none; stroke-width:1.6; }
        .value-line.active { stroke-width:2.6; }
        .chart-canvas { width:100%; border:1px solid var(--divider-color); border-radius:9px; overflow:hidden; background:var(--card-background-color); min-height:320px; position:relative; }
        .chart-canvas > .muted { padding:30px 10px; text-align:center; }
        .chart-canvas svg { display:block; width:100%; height:auto; }
        .plot-bg { fill:var(--secondary-background-color); opacity:.35; }
        .grid-line { stroke:var(--divider-color); stroke-width:1; }
        .axis-label { font-size:9px; fill:var(--secondary-text-color); }
        .axis-label.current { fill:#1565c0; font-weight:600; }
        .axis-label.learned { fill:#e65100; font-weight:600; }
        .threshold-line.current { stroke:#1565c0; stroke-width:1.6; }
        .threshold-line.learned { stroke:#e65100; stroke-width:1.6; stroke-dasharray:5,4; }
        .selection-hit { fill:transparent; pointer-events:all; cursor:crosshair; touch-action:none; }
        .selection-rect { fill:rgba(30,136,229,0.18); stroke:none; pointer-events:none; }
        .range-handle line { stroke:#1565c0; stroke-width:2.4; }
        .range-handle circle { fill:#1565c0; cursor:ew-resize; }
        .range-handle .handle-hit { fill:transparent; pointer-events:all; cursor:ew-resize; touch-action:none; }
        .selection-toolbar { padding:9px 10px; border-top:1px solid var(--divider-color); background:var(--secondary-background-color); display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
        .selection-toolbar.muted { color:var(--secondary-text-color); font-size:12px; }
        .selection-range { font-size:12px; color:var(--secondary-text-color); white-space:nowrap; }
        .selection-panel { flex-direction:column; align-items:stretch; }
        .selection-buttons { display:flex; gap:5px; flex-wrap:wrap; }
        .selection-buttons button { min-height:36px; padding:5px 10px; font-size:12px; }
        .learn-status { font-size:12px; margin-bottom:6px; }
        .learn-status.ok { color:var(--state-active-color,#2e7d32); }
        .learn-status.warn { color:var(--error-color,#c62828); }
        .learn-status.muted { color:var(--secondary-text-color); }
        .threshold-line.learned-unsafe { stroke:#c62828; stroke-width:1.6; stroke-dasharray:2,3; }
        .axis-label.learned-unsafe { fill:#c62828; font-weight:600; }
        .threshold-line.noise-ceiling { stroke:var(--secondary-text-color); stroke-width:1; stroke-dasharray:1,3; opacity:.7; }
        .axis-label.noise-ceiling { fill:var(--secondary-text-color); opacity:.85; }
        .chart-legend { display:flex; flex-wrap:wrap; gap:10px; margin-top:8px; font-size:11px; color:var(--secondary-text-color); align-items:center; }
        .chart-legend span { display:flex; align-items:center; gap:4px; }
        .chart-legend .swatch { width:12px; height:12px; border-radius:3px; display:inline-block; }
        .chart-legend .swatch.present { background:rgba(46,125,50,.5); }
        .chart-legend .swatch.not_present { background:rgba(120,120,120,.4); }
        .chart-legend .swatch.unknown { background:rgba(255,193,7,.4); }
        .chart-legend .lineswatch { width:16px; height:0; border-top:3px solid; display:inline-block; }
        .chart-legend .lineswatch.current { border-color:#1565c0; }
        .chart-legend .lineswatch.learned { border-color:#e65100; border-top-style:dashed; }
        .chart-legend .lineswatch.noise { border-color:var(--secondary-text-color); border-top-style:dotted; }
        @media (max-width: 700px) {
          :host { padding:8px; }
          h1 { font-size:22px; }
          .grid { display:block; }
          .card { margin-bottom:10px; padding:12px; }
          .training { align-items:stretch; }
          .training label { width:100%; margin-top:3px; }
          .training select { width:100%; }
          .duration { width:100%; }
          .duration input { flex:1; width:auto; }
          .history-fields { grid-template-columns:1fr; }
          .history-fields input,.history-fields select { width:100%; }
          .controls button,.export button { flex:1 1 100%; }
          .stats { grid-template-columns:1fr 1fr 1fr; gap:5px; }
          .stat { padding:8px 6px; font-size:11px; }
          .stat b { font-size:15px; }
          .table-scroll { display:none; }
          .mobile-gates { display:block; }
          .chart-controls select { flex:1 1 45%; min-width:0; }
        }
      </style>
      <div class="wrap">
        <div id="error" role="alert" class="notice warn" hidden></div>
        <h1>LD2410 Tuner</h1>
        <div class="subtitle">Automatic estimates learn from signal patterns over time and carry confidence scores. Add empty-room, moving and quiet-sitting examples to improve them. Learn prioritizes reliable presence across sessions; human labels always take priority over lower-confidence estimates. Inferred data proportions do not block Apply.</div>
        <div class="grid" id="grid"></div>
      </div>`;
    // If a poll landed while a field was focused, it's queued instead of
    // applied (see _load). Once focus leaves the panel, catch up on that
    // queued redraw so things don't just go stale until the next poll.
    this.shadowRoot.addEventListener("focusout", () => {
      setTimeout(() => {
        if (this._redrawPending && !this._isEditing()) {
          this._redrawPending = false;
          this._draw();
        }
      }, 0);
    });
  }

  async _call(type, data={}, timeout=30000) {
    let timer;
    try {
      return await Promise.race([
        this._hass.callWS({type:`ld2410_tuner/${type}`, ...data}),
        new Promise((_, reject) => { timer = setTimeout(() => reject(new Error("Request timed out. Refresh before retrying a change; it may have completed.")), timeout); }),
      ]);
    } finally { clearTimeout(timer); }
  }

  _showError(message) {
    const el = this.shadowRoot?.querySelector("#error");
    if (el) { el.textContent = message; el.hidden = !message; }
  }

  async _action(button, action) {
    button.disabled = true;
    try { await action(); this._showError(""); }
    catch (err) { this._showError(err?.message || String(err)); }
    finally { button.disabled = false; }
  }

  async _load() {
    if (!this._hass || this._loading) return;
    this._loading = true;
    try {
      this._data = await this._call("snapshot");
      this._showError("");
      this._loaded = true;
      if (this._isEditing()) {
        this._redrawPending = true;
      } else {
        this._draw();
      }
    } catch (err) {
      this._showError(`Unable to refresh: ${err?.message || err}`);
      if (!this._loaded) {
        const grid = this.shadowRoot?.querySelector("#grid");
        if (grid) grid.innerHTML = `<div class="card">Unable to load LD2410 Tuner: ${this._esc(err?.message || err)}</div>`;
      }
    } finally { this._loading = false; }
  }

  async _export(id, format) {
    try {
      const data = await this._call("export", {device_id:id});
      if (format === "csv") this._download(this._csv(data), `ld2410-tuner-${id}.csv`, "text/csv");
      else this._download(JSON.stringify(data, null, 2), `ld2410-tuner-${id}.json`, "application/json");
    } catch (err) { alert(`Export failed: ${err?.message || err}`); }
  }

  _download(content, filename, type) {
    const blob = new Blob([content], {type});
    const file = new File([blob], filename, {type});
    if (navigator.share && navigator.canShare && navigator.canShare({files:[file]})) {
      navigator.share({title:"LD2410 Tuner export", files:[file]}).catch(() => {});
      return;
    }
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a"); a.href=url; a.download=filename; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  _csv(data) {
    const rows = [["device","gate","kind","present_samples","not_present_samples","present_p50","present_p95","present_p99","present_max","not_present_p50","not_present_p95","not_present_p99","not_present_max","current_threshold","learned_threshold","sensitivity","specificity","false_positive_rate","false_negative_rate","noise_floor_p99","noise_ceiling","safety_margin","status","auto_used","auto_present_samples","auto_not_present_samples"]];
    for (const [id,d] of Object.entries(data.devices||{})) {
      for (let g=0; g<9; g++) for (const kind of ["move","still"]) {
        const key=`g${g}_${kind}`, ps=d.histogram_stats?.[key]?.present||{}, ns=d.histogram_stats?.[key]?.not_present||{};
        const p=d.last_learning?.proposals?.[key]||{};
        rows.push([d.name,`G${g}`,kind,ps.count||0,ns.count||0,ps.p50??"",ps.p95??"",ps.p99??"",ps.max??"",ns.p50??"",ns.p95??"",ns.p99??"",ns.max??"",d.current_thresholds?.[key]??"",p.threshold??"",p.sensitivity??"",p.specificity??"",p.false_positive_rate??"",p.false_negative_rate??"",p.noise_floor_p99??"",p.noise_ceiling??"",p.safety_margin??"",p.status||"",p.auto_used?"yes":"no",p.auto_samples?.present??0,p.auto_samples?.not_present??0]);
      }
      rows.push([]);
      rows.push(["AUTO SEGMENTS","state","timestamp","confidence","score"]);
      for (const seg of (d.auto_learning?.segments || [])) {
        rows.push([d.name,seg.state,new Date(Number(seg.start)*1000).toISOString(),seg.confidence??"",seg.score??""]);
      }
    }
    return rows.map(r=>r.map(v=>`"${String(v??"").replaceAll('"','""')}"`).join(",")).join("\n");
  }

  _sectionCollapsed(id, key) {
    const k = `${id}:${key}`;
    if (!this._sectionState.has(k)) this._sectionState.set(k, !!SECTION_DEFAULTS[key]);
    return this._sectionState.get(k);
  }

  _setSectionCollapsed(id, key, val) { this._sectionState.set(`${id}:${key}`, val); }

  _section(id, key, title, innerHtml) {
    const collapsed = this._sectionCollapsed(id, key);
    return `<div class="subsection${collapsed?" collapsed":""}" data-section="${key}">
      <div class="subsection-head" data-action="section-toggle" data-section="${key}"><span>${this._esc(title)}</span><button class="sub-toggle" aria-expanded="${!collapsed}">${collapsed?"▸":"▾"}</button></div>
      <div class="subsection-body">${innerHtml}</div>
    </div>`;
  }

  _defaultKindAndGate(d) {
    const top = d.auto_learning?.last?.top_gates?.[0]?.key;
    const m = top ? /^g(\d)_(move|still)$/.exec(top) : null;
    return m ? {gate: Number(m[1]), kind: m[2]} : {gate: 0, kind: "move"};
  }

  _chartHtml(id, d) {
    let cs = this._chartState.get(id);
    if (!cs) { cs = {...this._defaultKindAndGate(d), hours:6}; this._chartState.set(id, cs); }
    const kindOptions = ["move","still"].map(k=>`<option value="${k}" ${k===cs.kind?"selected":""}>${k==="move"?"Movement":"Still"}</option>`).join("");
    const gateOptions = Array.from({length:9},(_,g)=>g).map(g=>`<option value="${g}" ${g===cs.gate?"selected":""}>Gate ${g}</option>`).join("");
    const rangeOptions = CHART_RANGES.map(r=>`<option value="${r.hours}" ${r.hours===cs.hours?"selected":""}>${this._esc(r.label)}</option>`).join("");
    const gateChips = Array.from({length:9},(_,g)=>g).map(g=>`<button type="button" class="gate-chip${g===cs.gate?" active":""}" data-action="chart-pick-gate" data-gate="${g}" style="--chip-color:${GATE_COLORS[g]}">G${g}</button>`).join("");
    return `<div class="chart-controls">
        <label>Kind<br><select data-action="chart-kind">${kindOptions}</select></label>
        <label>Highlight gate<br><select data-action="chart-gate">${gateOptions}</select></label>
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
      <div class="muted" style="margin-top:6px">All 9 gates of the selected kind are shown together, each in its own color (click a G0–G8 chip or use "Highlight gate" to bring one to the front with its shaded band). Drag across the chart to select a time range — two handles appear so you can fine-tune each end — then use the toolbar below the chart to set its status.</div>`;
  }

  _chartKeys(cs) { return Array.from({length:9},(_,g)=>`g${g}_${cs.kind}`); }

  _maybeFetchChart(id, d) {
    const cs = this._chartState.get(id);
    if (!cs) return;
    this._renderChartCanvas(id);
    if (this._collapsed.has(id) || this._sectionCollapsed(id, "chart")) return;
    if (cs.loading || cs.error) return;
    const revision = d.history?.revision || 0;
    if (cs.loadedRevision !== revision) cs.cache?.clear();
    if (cs.data && cs.loadedKind === cs.kind && cs.loadedHours === cs.hours && cs.loadedRevision === revision) return;
    this._fetchChartData(id);
  }

  _queueChartCall(call) {
    return new Promise((resolve, reject) => {
      this._chartQueue.push({call, resolve, reject});
      this._drainChartQueue();
    });
  }

  _drainChartQueue() {
    while (this._activeCharts < 2 && this._chartQueue.length) {
      const {call, resolve, reject} = this._chartQueue.shift();
      this._activeCharts++;
      Promise.resolve().then(call).then(resolve, reject).finally(() => {
        this._activeCharts--;
        this._drainChartQueue();
      });
    }
  }

  async _fetchChartData(id, refresh=false) {
    const cs = this._chartState.get(id);
    if (!cs) return;
    if (refresh) { cs.end = null; cs.cache?.clear(); cs.selection = null; cs.panelOpen = false; }
    const {kind, hours} = cs;
    const end = cs.end;
    const revision = this._data?.devices?.[id]?.history?.revision || 0;
    const key = JSON.stringify([kind, hours, end, revision]);
    if (cs.loading && cs.pendingKey === key) return;
    const request = (cs.request || 0) + 1;
    cs.request = request; cs.pendingKey = key;
    cs.cache ||= new Map();
    const cached = cs.cache.get(key);
    if (cached) {
      cs.data = cached; cs.loadedKind = kind; cs.loadedHours = hours;
      cs.loadedRevision = revision; cs.error = null; cs.loading = false;
      this._renderChartCanvas(id);
      return;
    }
    cs.loading = true; cs.error = null;
    this._renderChartCanvas(id);
    try {
      const data = await this._queueChartCall(() => {
        if (request !== cs.request || !this.isConnected) return null;
        return this._call("history_series_multi", {
          device_id:id, keys:this._chartKeys({kind}), hours,
          ...(end != null ? {end} : {}),
        }, 15000);
      });
      if (request !== cs.request || !data) return;
      cs.data = data; cs.end = data.end;
      cs.loadedKind = kind; cs.loadedHours = hours; cs.loadedRevision = revision;
      cs.loadedAt = Date.now(); cs.error = null;
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
  }

  _renderChartCanvas(id, force=false) {
    const el = this.shadowRoot?.querySelector(`[data-chart-canvas="${CSS.escape(id)}"]`);
    if (!el) return;
    const cs = this._chartState.get(id);
    const d = this._data?.devices?.[id];
    if (!cs || !d) { el.innerHTML = `<div class="muted">Unavailable</div>`; return; }
    const active = this.shadowRoot.activeElement;
    if (this._dragging || (cs.panelOpen && el.contains(active) && active?.tagName === "INPUT")) return;
    const card = el.closest("[data-device-id]");
    for (const [action,value] of [["chart-kind",cs.kind],["chart-gate",cs.gate],["chart-range",cs.hours]]) {
      const control = card.querySelector(`[data-action="${action}"]`);
      if (control && control.value !== String(value)) control.value = String(value);
    }
    this._updateGateChipHighlight(card, cs.gate);
    const activeKey = `g${cs.gate}_${cs.kind}`;
    const signature = JSON.stringify([cs.gate,cs.kind,cs.hours,cs.loadedKind,cs.loadedHours,cs.loading,cs.error,cs.selection,cs.panelOpen,d.current_thresholds?.[activeKey],d.last_learning?.proposals]);
    if (!force && el._renderedData === cs.data && el._renderedState === signature) return;
    el._renderedData = cs.data; el._renderedState = signature;
    const matches = cs.data && cs.loadedKind === cs.kind && cs.loadedHours === cs.hours;
    const status = this.shadowRoot.querySelector(`[data-chart-status="${CSS.escape(id)}"]`);
    if (status) {
      const windowText = matches ? `Window ends ${new Date(cs.data.end*1000).toLocaleString()}. ${cs.data.bucket_seconds || "—"}s buckets: mean line, sampled min–max band. Refresh moves to latest.` : "";
      status.textContent = cs.error ? `Couldn't refresh history: ${cs.error}${matches ? " Showing the previous window." : ""}` : `${cs.loading ? "Loading history… " : ""}${windowText}`;
    }
    if (!matches) {
      el.innerHTML = `<div class="muted">${cs.error ? "History unavailable. Use Refresh to retry." : "Loading history…"}</div>`;
      return;
    }
    const anyPoints = Object.values(cs.data.series||{}).some(series=>series.points?.length);
    if (!anyPoints) { el.innerHTML = `<div class="muted">No recorded history in this window.</div>`; return; }
    try {
      el.innerHTML = this._buildChartSvg(d, cs);
      this._wireChartSelection(id, el, d, cs);
    } catch (err) {
      el.innerHTML = `<div class="muted">Unable to render chart: ${this._esc(err?.message || err)}</div>`;
    }
  }

  _falsePositiveSourcesHtml(learning) {
    if (learning?.status !== "unsafe") return "";
    const sources = Object.entries(learning.proposals || {})
      .filter(([key, proposal]) => /^g[0-8]_(move|still)$/.test(key) && proposal.false_positives > 0)
      .sort((a, b) => b[1].false_positives - a[1].false_positives)
      .slice(0, 3)
      .map(([key, proposal]) => {
        const [, gate, kind] = key.match(/^g([0-8])_(move|still)$/);
        return `Gate ${gate} ${kind === "move" ? "Movement" : "Still"} at ${Math.round(proposal.threshold)}: ${proposal.false_positives} / ${proposal.not_present_samples} empty-room samples`;
      });
    return sources.length ? `<div class="false-positive-sources">Largest per-gate false-positive counts: ${this._esc(sources.join("; "))}. Gates can trigger on the same samples; these counts must not be added.</div>` : "";
  }

  _learnStatusHtml(proposal, cs, learning) {
    const label = `Gate ${cs.gate} · ${cs.kind==="move"?"Movement":"Still"}`;
    if (!proposal) return `<div class="learn-status muted">${this._esc(label)}: click "Learn thresholds" to compute a recommendation.</div>`;
    const noiseNote = (proposal.noise_ceiling != null)
      ? ` Highest ${proposal.noise_source==="automatic"?"estimated":"labelled"} NOT PRESENT sample seen: ${Math.round(proposal.noise_ceiling)} (99th percentile: ${Math.round(proposal.noise_floor_p99)}).`
      : "";
    if (proposal.status === "provisional") return `<div class="learn-status warn">${this._esc(label)}: saved threshold ${Math.round(proposal.threshold)} was produced by the previous learner. Learn again with the current model before Apply.</div>`;
    if (proposal.status === "ok" && proposal.evidence_basis === "automatic") return `<div class="learn-status muted">${this._esc(label)}: estimated threshold ${Math.round(proposal.threshold)} from confidence-weighted observations. No human-labelled accuracy measurement yet.${noiseNote}</div>`;
    if (proposal.status === "ok" && proposal.role === "unchanged") return `<div class="learn-status muted">${this._esc(label)}: no usable observations for this gate; its current threshold is preserved.</div>`;
    if (proposal.status === "ok" && proposal.role === "suppressed") return `<div class="learn-status muted">${this._esc(label)}: threshold 100 suppresses this gate; its observed background requires suppression; inspect the device-wide results.</div>`;
    if (proposal.status === "ok") return `<div class="learn-status ok">${this._esc(label)}: learned threshold ${Math.round(proposal.threshold)} — this gate detects ${Math.round((proposal.sensitivity||0)*100)}% of human-labelled training samples, ${proposal.false_positives||0} exception(s) out of ${proposal.not_present_samples} not-present samples.${noiseNote}</div>`;
    if (proposal.status === "unsafe") {
      const gateResult = proposal.not_present_samples > 0 && proposal.false_positives != null
        ? ` This gate alone triggers on ${proposal.false_positives} / ${proposal.not_present_samples} human-labelled empty-room samples.`
        : " No human-labelled empty-room measurement is available for this gate.";
      return `<div class="learn-status warn"><div>${this._esc(label)}: candidate threshold ${Math.round(proposal.threshold)}.${this._esc(gateResult)}${noiseNote}</div><div><b>Combined device recommendation unsafe</b> — ${this._esc(proposal.message || "The combined thresholds did not meet the learning targets")}. These failure counts cover all enabled gates, not just this gate.</div>${this._falsePositiveSourcesHtml(learning)}<div>Candidate shown as a red dashed line; the combined recommendation has not been applied.</div></div>`;
    }
    return `<div class="learn-status warn">${this._esc(label)}: not enough data yet — ${this._esc(proposal.message||"need more present and not-present samples")}.</div>`;
  }

  _buildChartSvg(d, cs) {
    const data = cs.data;
    const activeKey = `g${cs.gate}_${cs.kind}`;
    const currentThreshold = d.current_thresholds?.[activeKey];
    const learnedProposal = d.last_learning?.proposals?.[activeKey];
    // A proposal marked "unsafe" still carries a computed threshold - it was
    // previously hidden outright whenever status wasn't "ok", which meant a
    // device with overlapping present/not-present data (exactly the sort of
    // device someone would be using this chart to investigate) would never
    // show a learned line at all, no matter how much data you gave it.
    const learnedThreshold = (learnedProposal && learnedProposal.threshold != null) ? learnedProposal.threshold : null;
    const learnedUnsafe = learnedProposal && learnedProposal.status !== "ok";
    // The single highest NOT_PRESENT sample ever seen for this gate - shown
    // so an isolated spike dominating the learned threshold is visible at a
    // glance, rather than being an invisible number behind the math.
    const noiseCeiling = learnedProposal?.noise_ceiling ?? null;

    const span = Math.max(1, data.end - data.start);
    const xScale = t => CHART_MARGIN.left + ((t - data.start) / span) * CHART_PLOT_W;
    const yScale = v => CHART_MARGIN.top + (1 - Math.max(0, Math.min(100, v)) / 100) * CHART_PLOT_H;
    const thresholdLine = (value, style, label) => {
      if (value == null || !Number.isFinite(Number(value))) return "";
      const y = yScale(Number(value));
      return `<line x1="${CHART_MARGIN.left}" y1="${y}" x2="${CHART_W-CHART_MARGIN.right}" y2="${y}" class="threshold-line ${style}"></line><text x="${CHART_MARGIN.left+4}" y="${Math.max(9,y-3)}" class="axis-label ${style}">${this._esc(label)} ${this._fmt(value)}</text>`;
    };

    const bandColor = {present:"rgba(46,125,50,0.18)", not_present:"rgba(120,120,120,0.14)", unknown:"rgba(255,193,7,0.14)"};
    const bands = (data.labels||[]).map(l => {
      const x0 = xScale(Math.max(l.start, data.start));
      const x1 = xScale(Math.min(l.end, data.end));
      const fill = bandColor[l.state];
      if (!fill || x1 <= x0) return "";
      return `<rect x="${x0.toFixed(1)}" y="${CHART_MARGIN.top}" width="${(x1-x0).toFixed(1)}" height="${CHART_PLOT_H}" fill="${fill}"></rect>`;
    }).join("");

    const yTicks = [0,25,50,75,100].map(v=>{
      const y=yScale(v);
      return `<line x1="${CHART_MARGIN.left}" y1="${y.toFixed(1)}" x2="${CHART_W-CHART_MARGIN.right}" y2="${y.toFixed(1)}" class="grid-line"></line><text x="${CHART_MARGIN.left-6}" y="${(y+3).toFixed(1)}" class="axis-label" text-anchor="end">${v}</text>`;
    }).join("");

    const spanHours = span / 3600;
    const tickCount = 5;
    const timeFmt = t => {
      const dt = new Date(t*1000);
      return dt.toLocaleString(undefined,{...(spanHours >= 12 ? {month:"short",day:"numeric"} : {}),hour:"2-digit",minute:"2-digit"});
    };
    const xTicks = Array.from({length:tickCount}, (_,i) => {
      const t = data.start + (i/(tickCount-1)) * span;
      const x = xScale(t);
      return `<line x1="${x.toFixed(1)}" y1="${CHART_MARGIN.top}" x2="${x.toFixed(1)}" y2="${CHART_H-CHART_MARGIN.bottom}" class="grid-line"></line><text x="${x.toFixed(1)}" y="${CHART_H-CHART_MARGIN.bottom+16}" class="axis-label" text-anchor="${i===0?"start":i===tickCount-1?"end":"middle"}">${this._esc(timeFmt(t))}</text>`;
    }).join("");

    // Every non-highlighted gate of this kind gets a thin, muted line for
    // silhouette/comparison; the highlighted gate gets the full treatment
    // (shaded min-max band, bold line).
    const segments = points => {
      const groups = [];
      for (const point of points) {
        const last = groups.at(-1);
        if (!last || (data.bucket_seconds && point.t-last.at(-1).t > data.bucket_seconds*2)) groups.push([point]);
        else last.push(point);
      }
      return groups;
    };
    let otherLines = "";
    for (let g=0; g<9; g++) {
      if (g === cs.gate) continue;
      const s = data.series?.[`g${g}_${cs.kind}`];
      if (!s || !s.points.length) continue;
      const path = segments(s.points).map(points=>points.map((p,i)=>`${i===0?"M":"L"}${xScale(p.t).toFixed(1)},${yScale(p.avg).toFixed(1)}`).join(" ")).join(" ");
      otherLines += `<path d="${path}" class="gate-line" style="stroke:${GATE_COLORS[g]}"></path>`;
    }

    const activeSeries = data.series?.[activeKey];
    let activeBandSvg = "", activeLineSvg = "";
    if (activeSeries && activeSeries.points.length) {
      for (const points of segments(activeSeries.points)) {
        const linePath = points.map((p,i)=>`${i===0?"M":"L"}${xScale(p.t).toFixed(1)},${yScale(p.avg).toFixed(1)}`).join(" ");
        const bandTop = points.map(p=>`${xScale(p.t).toFixed(1)},${yScale(p.max).toFixed(1)}`).join(" L ");
        const bandBottom = points.slice().reverse().map(p=>`${xScale(p.t).toFixed(1)},${yScale(p.min).toFixed(1)}`).join(" L ");
        activeBandSvg += `<path d="M ${bandTop} L ${bandBottom} Z" class="value-band" style="fill:${GATE_COLORS[cs.gate]}"></path>`;
        activeLineSvg += `<path d="${linePath}" class="value-line active" style="stroke:${GATE_COLORS[cs.gate]}"></path>`;
        if (points.length === 1) activeLineSvg += `<circle cx="${xScale(points[0].t)}" cy="${yScale(points[0].avg)}" r="2" fill="${GATE_COLORS[cs.gate]}"></circle>`;
      }
    }

    // Persistent time-range selection: two independently draggable handles
    // plus a small toolbar (shown outside the SVG) with a fine-tune panel.
    let selectionSvg = `<rect data-role="selection-rect" class="selection-rect" x="0" y="${CHART_MARGIN.top}" width="0" height="${CHART_PLOT_H}" style="display:none"></rect>`;
    if (cs.selection) {
      const xStart = xScale(cs.selection.start), xEnd = xScale(cs.selection.end);
      const x0 = Math.min(xStart, xEnd), x1 = Math.max(xStart, xEnd);
      const handle = (role, x) => `<g data-role="handle-${role}" class="range-handle">
          <line x1="${x.toFixed(1)}" y1="${CHART_MARGIN.top}" x2="${x.toFixed(1)}" y2="${CHART_H-CHART_MARGIN.bottom}"></line>
          <circle cx="${x.toFixed(1)}" cy="${(CHART_MARGIN.top+CHART_PLOT_H/2).toFixed(1)}" r="7"></circle>
          <rect class="handle-hit" x="${(x-11).toFixed(1)}" y="${CHART_MARGIN.top}" width="22" height="${CHART_PLOT_H}"></rect>
        </g>`;
      selectionSvg = `<rect data-role="selection-rect" class="selection-rect" x="${x0.toFixed(1)}" y="${CHART_MARGIN.top}" width="${(x1-x0).toFixed(1)}" height="${CHART_PLOT_H}"></rect>
        ${handle("start", xStart)}
        ${handle("end", xEnd)}`;
    }

    const toolbarHtml = this._selectionToolbarHtml(cs);

    return `${this._learnStatusHtml(learnedProposal, cs, d.last_learning)}<svg viewBox="0 0 ${CHART_W} ${CHART_H}" preserveAspectRatio="xMidYMid meet">
      <rect x="${CHART_MARGIN.left}" y="${CHART_MARGIN.top}" width="${CHART_PLOT_W}" height="${CHART_PLOT_H}" class="plot-bg"></rect>
      ${bands}
      ${yTicks}
      ${xTicks}
      ${otherLines}
      ${activeBandSvg}
      ${activeLineSvg}
      ${thresholdLine(learnedThreshold, learnedUnsafe?"learned-unsafe":"learned", learnedUnsafe?"Learned (unsafe)":"Learned")}
      ${thresholdLine(currentThreshold, "current", "Current")}
      ${thresholdLine(noiseCeiling, "noise-ceiling", "Noise max")}
      <rect data-role="selection-hit" class="selection-hit" x="${CHART_MARGIN.left}" y="${CHART_MARGIN.top}" width="${CHART_PLOT_W}" height="${CHART_PLOT_H}"></rect>
      ${selectionSvg}
    </svg>${toolbarHtml}`;
  }

  _toLocalInputValue(ts) {
    const dt = new Date(ts*1000);
    const pad = n => String(n).padStart(2,"0");
    return `${dt.getFullYear()}-${pad(dt.getMonth()+1)}-${pad(dt.getDate())}T${pad(dt.getHours())}:${pad(dt.getMinutes())}`;
  }

  _selectionToolbarHtml(cs) {
    if (!cs.selection) {
      return `<div class="selection-toolbar muted">Drag across the chart to select a time range to label. Once selected, drag either end to fine-tune.</div>`;
    }
    const fmt = t => new Date(t*1000).toLocaleString(undefined,{month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"});
    if (!cs.panelOpen) {
      return `<div class="selection-toolbar">
        <span class="selection-range">${this._esc(fmt(cs.selection.start))} → ${this._esc(fmt(cs.selection.end))}</span>
        <button type="button" class="fb-btn" data-action="selection-finetune">Fine-tune &amp; set status</button>
        <button type="button" class="fb-btn" data-action="selection-cancel">Cancel</button>
      </div>`;
    }
    return `<div class="selection-toolbar selection-panel">
      <div class="history-fields" style="grid-template-columns:1fr 1fr;">
        <label>From<input type="datetime-local" data-role="selection-from" value="${this._toLocalInputValue(cs.selection.start)}"></label>
        <label>To<input type="datetime-local" data-role="selection-to" value="${this._toLocalInputValue(cs.selection.end)}"></label>
      </div>
      <div class="selection-buttons">
        <button type="button" class="fb-btn" data-action="selection-label" data-label="present">Present</button>
        <button type="button" class="fb-btn" data-action="selection-label" data-label="not_present">Not present</button>
        <button type="button" class="fb-btn" data-action="selection-label" data-label="unknown">Unknown</button>
        <button type="button" class="fb-btn" data-action="selection-exit">Exit</button>
      </div>
    </div>`;
  }

  // Two independently draggable handles mark a time range; a small toolbar
  // below the chart lets you cancel, or open a panel with editable
  // start/end fields (kept in sync with the handles both ways) and buttons
  // to commit PRESENT / NOT PRESENT / UNKNOWN for that range.
  _beginDrag(onMove, onUp) {
    this._endDrag?.();
    this._dragging = true;
    const end = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", cancel);
      window.removeEventListener("blur", cancel);
      this._dragging = false;
      this._endDrag = null;
    };
    const up = event => { end(); onUp(event); };
    const cancel = () => { end(); this._draw(); };
    this._endDrag = end;
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", cancel);
    window.addEventListener("blur", cancel);
  }

  _wireChartSelection(id, container, d, cs) {
    const svg = container.querySelector("svg");
    if (!svg) return;
    const data = cs.data;
    const span = Math.max(1, data.end - data.start);
    const xScale = t => CHART_MARGIN.left + ((t - data.start) / span) * CHART_PLOT_W;
    const xToTime = x => data.start + ((x - CHART_MARGIN.left) / CHART_PLOT_W) * span;
    const pxFromClientX = (clientX) => {
      const rect = svg.getBoundingClientRect();
      const scale = rect.width ? CHART_W / rect.width : 1;
      return Math.max(CHART_MARGIN.left, Math.min(CHART_W - CHART_MARGIN.right, (clientX - rect.left) * scale));
    };
    const clientXOf = (ev) => ev.touches ? ev.touches[0].clientX : (ev.changedTouches ? ev.changedTouches[0].clientX : ev.clientX);

    const updateHandleVisual = (which) => {
      const x = xScale(cs.selection[which]);
      const g = container.querySelector(`[data-role="handle-${which}"]`);
      if (g) {
        const line = g.querySelector("line"), circle = g.querySelector("circle"), hit = g.querySelector(".handle-hit");
        if (line) { line.setAttribute("x1", x.toFixed(1)); line.setAttribute("x2", x.toFixed(1)); }
        if (circle) circle.setAttribute("cx", x.toFixed(1));
        if (hit) hit.setAttribute("x", (x-11).toFixed(1));
      }
      const rect = container.querySelector('[data-role="selection-rect"]');
      if (rect) {
        const x0 = xScale(cs.selection.start), x1 = xScale(cs.selection.end);
        rect.setAttribute("x", Math.min(x0,x1).toFixed(1));
        rect.setAttribute("width", Math.abs(x1-x0).toFixed(1));
      }
    };
    const syncPanelFields = () => {
      if (!cs.panelOpen) return;
      const fromInput = container.querySelector('[data-role="selection-from"]');
      const toInput = container.querySelector('[data-role="selection-to"]');
      if (fromInput) fromInput.value = this._toLocalInputValue(cs.selection.start);
      if (toInput) toInput.value = this._toLocalInputValue(cs.selection.end);
    };

    container.querySelectorAll('[data-role="handle-start"], [data-role="handle-end"]').forEach(g=>{
      const which = g.dataset.role === "handle-start" ? "start" : "end";
      g.onpointerdown = (ev) => {
        ev.preventDefault(); ev.stopPropagation();
        this._dragging = true;
        const onMove = (e2) => {
          e2.preventDefault();
          let t = xToTime(pxFromClientX(clientXOf(e2)));
          if (which === "start") t = Math.min(t, cs.selection.end - 1);
          else t = Math.max(t, cs.selection.start + 1);
          cs.selection[which] = t;
          updateHandleVisual(which);
          syncPanelFields();
        };
        const onUp = () => {
          window.removeEventListener("pointermove", onMove);
          this._dragging = false;
          this._renderChartCanvas(id);
        };
        this._beginDrag(onMove, onUp);
      };
    });

    // Dragging on empty plot space always starts a brand-new selection,
    // replacing any existing one (handles have their own listeners above
    // and stopPropagation, so a drag that starts on a handle never reaches
    // this).
    const hit = container.querySelector('[data-role="selection-hit"]');
    if (hit) {
      hit.onpointerdown = (ev) => {
        ev.preventDefault();
        this._dragging = true;
        const startX = pxFromClientX(clientXOf(ev));
        const selRect = container.querySelector('[data-role="selection-rect"]');
        const showTemp = (x0, x1) => {
          if (!selRect) return;
          selRect.style.display = "block";
          selRect.setAttribute("x", Math.min(x0,x1).toFixed(1));
          selRect.setAttribute("width", Math.abs(x1-x0).toFixed(1));
        };
        showTemp(startX, startX);
        const onMove = (e2) => {
          e2.preventDefault();
          showTemp(startX, pxFromClientX(clientXOf(e2)));
        };
        const onUp = (e2) => {
          window.removeEventListener("pointermove", onMove);
          this._dragging = false;
          const x = pxFromClientX(clientXOf(e2));
          const x0 = Math.min(startX, x), x1 = Math.max(startX, x);
          if (x1 - x0 < 4) { if (selRect) selRect.style.display = "none"; return; }
          cs.selection = {start: xToTime(x0), end: xToTime(x1)};
          cs.panelOpen = false;
          this._renderChartCanvas(id);
        };
        this._beginDrag(onMove, onUp);
      };
    }

    const finetuneBtn = container.querySelector('[data-action="selection-finetune"]');
    if (finetuneBtn) finetuneBtn.onclick = () => { cs.panelOpen = true; this._renderChartCanvas(id); };
    const cancelBtn = container.querySelector('[data-action="selection-cancel"]');
    if (cancelBtn) cancelBtn.onclick = () => { cs.selection = null; cs.panelOpen = false; this._renderChartCanvas(id); };
    const exitBtn = container.querySelector('[data-action="selection-exit"]');
    if (exitBtn) exitBtn.onclick = () => { cs.selection = null; cs.panelOpen = false; this._renderChartCanvas(id); };

    const fromInput = container.querySelector('[data-role="selection-from"]');
    if (fromInput) fromInput.oninput = () => {
      const t = Math.floor(new Date(fromInput.value).getTime()/1000);
      if (!Number.isFinite(t)) return;
      cs.selection.start = Math.min(t, cs.selection.end - 1);
      updateHandleVisual("start");
      if (cs.selection.start !== t) fromInput.value = this._toLocalInputValue(cs.selection.start);
    };
    const toInput = container.querySelector('[data-role="selection-to"]');
    if (toInput) toInput.oninput = () => {
      const t = Math.floor(new Date(toInput.value).getTime()/1000);
      if (!Number.isFinite(t)) return;
      cs.selection.end = Math.max(t, cs.selection.start + 1);
      updateHandleVisual("end");
      if (cs.selection.end !== t) toInput.value = this._toLocalInputValue(cs.selection.end);
    };

    container.querySelectorAll('[data-action="selection-label"]').forEach(btn=>{
      btn.onclick = async () => {
        const label = btn.dataset.label;
        const {start, end} = cs.selection;
        try {
          await this._call("label_history", {device_id:id, start:Math.floor(start), end:Math.floor(end), state:label});
          cs.selection = null; cs.panelOpen = false; cs.data = null; cs.cache?.clear(); cs.cache?.clear();
        } catch (err) { alert(`Unable to label history: ${err?.message||err}`); return; }
        await this._load();
      };
    });
  }

  _updateGateChipHighlight(card, gate) {
    card.querySelectorAll(".gate-chip").forEach(btn=>{
      btn.classList.toggle("active", Number(btn.dataset.gate) === gate);
    });
  }

  _draw() {
    const grid=this.shadowRoot.querySelector("#grid");
    for (const card of grid.querySelectorAll("[data-device-id]")) {
      const draft = {};
      card.querySelectorAll('[data-action="history-start"], [data-action="history-end"], [data-action="history-state"], [data-action="timeout-hours"], [data-action="timeout-minutes"]').forEach(el => { draft[el.dataset.action] = el.value; });
      this._drafts.set(card.dataset.deviceId, draft);
    }
    const charts = new Map([...grid.querySelectorAll("[data-device-id]")].map(card=>[card.dataset.deviceId, card.querySelector('[data-section="chart"]')]));
    const fragment = document.createDocumentFragment();
    const devices=this._data?.devices||{};
    if (!Object.keys(devices).length) { grid.innerHTML=`<div class="card">No LD2410 gate energy entities were discovered.</div>`; return; }
    for (const [id,d] of Object.entries(devices)) {
      if (!this._seenDevices.has(id)) {
        this._seenDevices.add(id);
        this._collapsed.add(id); // cards start collapsed
      }
      const card=document.createElement("div"); card.className="card"; card.dataset.deviceId=id;
      const state=d.training_state||"unknown", counts=d.sample_counts||{};
      const totalPresent=Object.values(counts).reduce((n,x)=>n+(x.present||0),0);
      const totalAbsent=Object.values(counts).reduce((n,x)=>n+(x.not_present||0),0);
      const rows=[], mobile=[];
      for(let g=0;g<9;g++){
        const keys=[`g${g}_move`,`g${g}_still`], mc=counts[keys[0]]||{}, sc=counts[keys[1]]||{}, mp=d.last_learning?.proposals?.[keys[0]], sp=d.last_learning?.proposals?.[keys[1]];
        rows.push(`<tr><td>G${g}</td><td>${this._fmt(d.current_thresholds?.[keys[0]])}</td><td class="${mp?.status==='ok'?'learned':''}">${this._fmt(mp?.threshold)}</td><td>${mc.present||0}/${mc.not_present||0}</td><td>${this._fmt(d.current_thresholds?.[keys[1]])}</td><td class="${sp?.status==='ok'?'learned':''}">${this._fmt(sp?.threshold)}</td><td>${sc.present||0}/${sc.not_present||0}</td></tr>`);
        mobile.push(`<div class="gate"><div class="gate-head"><span>Gate ${g}</span><span>Move / Still</span></div><div class="gate-grid"><div><span>Current</span>${this._fmt(d.current_thresholds?.[keys[0]])} / ${this._fmt(d.current_thresholds?.[keys[1]])}</div><div><span>Learned</span><b class="${mp?.status==='ok'||sp?.status==='ok'?'learned':''}">${this._fmt(mp?.threshold)} / ${this._fmt(sp?.threshold)}</b></div><div><span>Move P/N</span>${mc.present||0} / ${mc.not_present||0}</div><div><span>Still P/N</span>${sc.present||0} / ${sc.not_present||0}</div></div></div>`);
      }
      const warning=d.last_learning?.warnings?.length ? `<div class="notice ${d.last_learning.status==="unsafe"?"warn":""}">${d.last_learning.warnings.map(this._esc).join("<br>")}</div>` : "";
      const auto=d.auto_learning||{};
      const al=auto.last;
      const autoState=al?.state||"unknown";
      const autoPill=autoState==="unknown"?"UNKNOWN":autoState==="present"?"AUTO PRESENT":"AUTO NOT PRESENT";
      const top=al?.top_gates?.slice(0,3).map(x=>`${this._esc(x.key)} ${this._fmt(x.energy)}`).join(" · ")||"No confident classification yet";
      const canFeedback=autoState==="present"||autoState==="not_present";
      const fb=auto.feedback||{};
      const fbText=key=>{const f=fb[key];return f&&f.total?`${f.correct}/${f.total}`:"—";};
      const calib=auto.calibration||{};
      const calibBits=[];
      if (calib.present_bias) calibBits.push(`present threshold +${Number(calib.present_bias).toFixed(2)}`);
      if (calib.absent_bias) calibBits.push(`not present threshold +${Number(calib.absent_bias).toFixed(2)}`);
      const calibNote=calibBits.length?` · Calibration from feedback: ${calibBits.join(", ")}`:"";

      const isCollapsed=this._collapsed.has(id);

      const trainingHtml=`<div class="training"><label>Training state</label><select data-action="state"><option value="unknown" ${state==="unknown"?"selected":""}>UNKNOWN</option><option value="present" ${state==="present"?"selected":""}>PRESENT</option><option value="not_present" ${state==="not_present"?"selected":""}>NOT PRESENT</option></select><label>Timeout</label><div class="duration"><input data-action="timeout-hours" type="number" min="0" step="1" inputmode="numeric" placeholder="Hours"><span>h</span><input data-action="timeout-minutes" type="number" min="0" max="59" step="1" inputmode="numeric" placeholder="Minutes"><span>m</span></div>${d.training_expires_at?`<span class="area">Expires ${this._timeLeft(d.training_expires_at)}</span>`:""}</div>`;

      const historyHtml=`<div class="muted">Choose a time range from the stored ${d.history?.retention_days||30}-day history. This replaces any existing label in the selected range.</div><div class="history-fields"><label>From<input data-action="history-start" type="datetime-local"></label><label>To<input data-action="history-end" type="datetime-local"></label><label>Label<select data-action="history-state"><option value="present">PRESENT</option><option value="not_present">NOT PRESENT</option><option value="unknown">UNKNOWN / exclude</option></select></label></div><button data-action="history-apply" class="primary">Label time range</button>${(d.history?.labels||[]).length?`<div class="label-list">${d.history.labels.slice().sort((a,b)=>a.start-b.start).map(x=>`<div>${this._esc(new Date(Number(x.start)*1000).toLocaleString())} → ${this._esc(new Date(Number(x.end)*1000).toLocaleString())}: <b>${this._esc(x.state.replace("_"," ").toUpperCase())}</b></div>`).join("")}</div>`:""}`;

      const autoHtml=`<div class="auto-head"><span class="pill ${autoState}">${autoPill}</span><span class="muted">Training weight: 20% × confidence; human labels take priority</span></div><div class="auto-details"><div><span>Estimated confidence</span>${al?`${Math.round((al.confidence||0)*100)}%`:"—"}</div><div><span>Evidence source</span>${al?.basis==="human-guided"?"Labelled examples":"Background estimate"}</div><div><span>Segments</span>${auto.segments||0}</div></div><div class="muted" style="margin-top:7px">${this._esc(top)}</div><div class="muted">${auto.observations||0} estimates stored. Confidence is an estimate, not measured accuracy.</div><div class="feedback-row"><span class="muted">Was this reading right?</span><button class="fb-btn correct" data-action="auto-feedback" data-correct="true" ${canFeedback?"":"disabled"}>✓ Correct</button><button class="fb-btn incorrect" data-action="auto-feedback" data-correct="false" ${canFeedback?"":"disabled"}>✗ Incorrect</button></div><div class="muted" style="margin-top:5px">Feedback accuracy — present: ${fbText("present")}, not present: ${fbText("not_present")}${calibNote}</div>`;

      const detailsHtml=`<div class="stats"><div class="stat">Human gate samples · present<b>${totalPresent}</b></div><div class="stat">Human gate samples · absent<b>${totalAbsent}</b></div><div class="stat">Storage<b>Compressed</b></div></div><div class="table-scroll"><table><thead><tr><th>Gate</th><th>Move now</th><th>Move learned</th><th>Move P/N</th><th>Still now</th><th>Still learned</th><th>Still P/N</th></tr></thead><tbody>${rows.join("")}</tbody></table></div><div class="mobile-gates">${mobile.join("")}</div>${warning}${d.last_learning?`<div class="notice">Learning checks coverage across presence episodes, missed runs and false-trigger bursts. Estimates carry less weight than human labels. A gate at 100 is suppressed. Human labels take priority. Source proportions do not block Apply.</div>`:""}`;

      const measured=d.last_learning?.training;
      const validation=measured && (measured.present_samples || measured.not_present_samples) ? measured : null;
      const backtest=d.last_learning?.validation;
      const inferred=d.last_learning?.automatic_evidence;
      const inferenceHtml=inferred ? `<div class="notice">Automatic evidence: ${inferred.samples?.present||0} present / ${inferred.samples?.not_present||0} empty estimates. Mean confidence: ${inferred.mean_confidence?.present==null?"—":`${Math.round(inferred.mean_confidence.present*100)}%`} / ${inferred.mean_confidence?.not_present==null?"—":`${Math.round(inferred.mean_confidence.not_present*100)}%`}. Effective training weight: ${(inferred.effective_weight?.present||0).toFixed(1)} / ${(inferred.effective_weight?.not_present||0).toFixed(1)} human-sample equivalents.${inferred.deferred_samples?` ${inferred.deferred_samples} newer estimates await later human-labelled validation.`:""}</div>` : "";
      const validationHtml=(validation ? `<div class="notice">Human-labelled timed observations: <b>${validation.false_negatives??"—"} missed / ${validation.present_samples} presence samples</b> (${validation.present_samples ? (validation.sensitivity*100).toFixed(2)+"%" : "not measured"}; target 99.9%). Missed episodes: ${validation.missed_presence_episodes??"—"}; longest missed run: ${validation.longest_missed_run_samples??"—"} samples. False-trigger bursts: ${validation.false_trigger_bursts??"—"} (${validation.not_present_samples ? (validation.false_positive_rate*100).toFixed(2)+"% of empty samples" : "no human-labelled empty samples"}). ${d.last_learning.status==="ok"?"Recommendation available.":"The combined thresholds did not meet the targets."}</div>` : d.last_learning?.proposals && Object.keys(d.last_learning.proposals).length ? `<div class="notice">No human-labelled measurement is available. This recommendation uses confidence-weighted estimates; test it in the room.</div>` : "")+(backtest ? `<div class="notice">Earlier-data backtest: ${backtest.false_negatives??0} missed presence samples; ${backtest.false_positives??0} false triggers. The final recommendation uses all observations.</div>` : "")+this._falsePositiveSourcesHtml(d.last_learning)+inferenceHtml;
      const actionsHtml=`${validationHtml}${warning}<div class="controls"><button class="primary" data-action="learn">Learn thresholds</button><button data-action="apply" ${d.last_learning?.status==="ok"&&d.last_learning?.method==="human_priority_v4"?"":"disabled"}>Apply recommended thresholds</button><button data-action="clear">Clear data</button></div><div class="export"><button data-action="json">Export JSON</button><button data-action="csv">Export CSV</button></div>`;

      const bodyHtml=[
        this._section(id,"training","Training",trainingHtml),
        this._section(id,"chart","Gate visualization",this._chartHtml(id,d)),
        this._section(id,"history","Label past data",historyHtml),
        this._section(id,"auto","Automatic analysis & feedback",autoHtml),
        this._section(id,"details","Details & raw table",validationHtml+detailsHtml),
        this._section(id,"actions","Actions & export",actionsHtml),
      ].join("");

      card.innerHTML=`<div class="top" data-action="toggle-top"><div><div class="name">${this._esc(d.name)}<span class="pill ${autoState}" title="Latest automatic reading">${autoPill}${canFeedback?` · ${Math.round((al.confidence||0)*100)}%`:""}</span></div><div class="area">${this._esc(d.area_id||"No area")}</div></div><button class="toggle" data-action="toggle" aria-label="${isCollapsed?"Expand":"Collapse"}" aria-expanded="${!isCollapsed}">${isCollapsed?"▸":"▾"}</button></div>
        <div class="body${isCollapsed?" collapsed":""}">${bodyHtml}</div>`;

      const previousChart = charts.get(id);
      if (previousChart) card.querySelector('[data-section="chart"]').replaceWith(previousChart);

      const toggleBody=()=>{
        const willCollapse=!this._collapsed.has(id);
        if (willCollapse) this._collapsed.add(id); else this._collapsed.delete(id);
        const body=card.querySelector(".body");
        const btn=card.querySelector('[data-action="toggle"]');
        body.classList.toggle("collapsed", willCollapse);
        btn.textContent = willCollapse ? "▸" : "▾";
        btn.setAttribute("aria-expanded", String(!willCollapse));
        btn.setAttribute("aria-label", willCollapse ? "Expand" : "Collapse");
        if (!willCollapse) this._maybeFetchChart(id, d);
      };
      card.querySelector('[data-action="toggle"]').onclick=(e)=>{e.stopPropagation();toggleBody();};
      card.querySelector('[data-action="toggle-top"]').onclick=toggleBody;

      card.querySelectorAll('[data-action="section-toggle"]').forEach(head=>{
        head.onclick=()=>{
          const key=head.dataset.section;
          const willCollapse=!this._sectionCollapsed(id,key);
          this._setSectionCollapsed(id,key,willCollapse);
          const sub=head.closest(".subsection");
          sub.classList.toggle("collapsed", willCollapse);
          const btn=head.querySelector(".sub-toggle");
          btn.textContent = willCollapse ? "▸" : "▾";
          btn.setAttribute("aria-expanded", String(!willCollapse));
          if (key==="chart" && !willCollapse) this._maybeFetchChart(id, d);
        };
      });

      const timeoutHours=card.querySelector('[data-action="timeout-hours"]');
      const timeoutMinutes=card.querySelector('[data-action="timeout-minutes"]');
      const timeoutTotal=Number(d.training_timeout_seconds||0);
      timeoutHours.value=String(Math.floor(timeoutTotal/3600));
      timeoutMinutes.value=String(Math.floor((timeoutTotal%3600)/60));
      const saveTimeout=async()=>{
        const current=card.querySelector('[data-action="state"]').value;
        let hours=Math.max(0,parseInt(timeoutHours.value||"0",10)||0);
        let minutes=Math.max(0,Math.min(59,parseInt(timeoutMinutes.value||"0",10)||0));
        timeoutHours.value=String(hours); timeoutMinutes.value=String(minutes);
        if(current!=="unknown") await this._call("set_training_state",{device_id:id,state:current,timeout_seconds:hours*3600+minutes*60});
        else if(hours||minutes) await this._call("set_training_state",{device_id:id,state:current,timeout_seconds:0});
        await this._load();
      };
      card.querySelector('[data-action="state"]').onchange=e=>this._action(e.currentTarget,async()=>{await this._call("set_training_state",{device_id:id,state:e.target.value,timeout_seconds:e.target.value==="unknown"?0:(Number(timeoutHours.value||0)*3600+Number(timeoutMinutes.value||0)*60)});await this._load();});
      timeoutHours.onchange=e=>this._action(e.currentTarget,saveTimeout); timeoutMinutes.onchange=e=>this._action(e.currentTarget,saveTimeout);
      card.querySelector('[data-action="history-apply"]').onclick=async()=>{
        const from=card.querySelector('[data-action="history-start"]').value;
        const to=card.querySelector('[data-action="history-end"]').value;
        if(!from||!to){alert("Choose both a start and end time.");return;}
        const start=Math.floor(new Date(from).getTime()/1000), end=Math.floor(new Date(to).getTime()/1000);
        if(!Number.isFinite(start)||!Number.isFinite(end)||end<=start){alert("The end time must be after the start time.");return;}
        const label=card.querySelector('[data-action="history-state"]').value;
        if(!confirm(`Label this period ${label.replace("_"," ").toUpperCase()}?`)) return;
        try{await this._call("label_history",{device_id:id,start,end,state:label});this._chartState.get(id).data=null;this._chartState.get(id).cache?.clear();await this._load();}catch(err){alert(`Unable to label history: ${err?.message||err}`);}
      };
      card.querySelector('[data-action="learn"]').onclick=e=>this._action(e.currentTarget,async()=>{await this._call("learn",{device_id:id},120000);this._setSectionCollapsed(id,"details",false);await this._load();});
      card.querySelector('[data-action="apply"]').onclick=e=>this._action(e.currentTarget,async()=>{if(confirm("Apply these recommended thresholds? Inferred results are estimates; verify quiet presence and empty-room behaviour.")){const result=await this._call("apply",{device_id:id});await this._load();if(Object.keys(result.skipped||{}).length) throw new Error(`Applied ${Object.keys(result.applied||{}).length} thresholds. Incomplete: ${Object.entries(result.skipped).map(([key,reason])=>`${key}: ${reason}`).join("; ")}`);}});
      card.querySelector('[data-action="clear"]').onclick=e=>this._action(e.currentTarget,async()=>{if(confirm("Clear training, history and learned thresholds for this device?")){await this._call("clear",{device_id:id});this._chartState.delete(id);await this._load();}});
      card.querySelector('[data-action="json"]').onclick=()=>this._export(id,"json");
      card.querySelector('[data-action="csv"]').onclick=()=>this._export(id,"csv");
      card.querySelectorAll('[data-action="auto-feedback"]').forEach(btn=>{
        btn.onclick=async()=>{
          try { await this._call("auto_feedback", {device_id:id, correct: btn.dataset.correct==="true"}); }
          catch(err){ alert(`Unable to record feedback: ${err?.message||err}`); return; }
          await this._load();
        };
      });

      const chartKind=card.querySelector('[data-action="chart-kind"]');
      const chartGate=card.querySelector('[data-action="chart-gate"]');
      const chartRange=card.querySelector('[data-action="chart-range"]');
      const chartRefresh=card.querySelector('[data-action="chart-refresh"]');
      const cs=this._chartState.get(id);
      chartKind.onchange=()=>{cs.kind=chartKind.value; this._fetchChartData(id);};
      chartGate.onchange=()=>{cs.gate=Number(chartGate.value); this._updateGateChipHighlight(card,cs.gate); this._renderChartCanvas(id);};
      chartRange.onchange=()=>{cs.hours=Number(chartRange.value); cs.selection=null; cs.panelOpen=false; this._fetchChartData(id);};
      chartRefresh.onclick=()=>{this._fetchChartData(id,true);};
      card.querySelectorAll('[data-action="chart-pick-gate"]').forEach(btn=>{
        btn.onclick=()=>{
          cs.gate=Number(btn.dataset.gate);
          chartGate.value=String(cs.gate);
          this._updateGateChipHighlight(card,cs.gate);
          this._renderChartCanvas(id);
        };
      });

      const draft = this._drafts.get(id);
      if (draft) Object.entries(draft).forEach(([action,value]) => { const field=card.querySelector(`[data-action="${action}"]`); if (field) field.value=value; });
      fragment.appendChild(card);
    }
    grid.replaceChildren(fragment);
    for (const [id,d] of Object.entries(devices)) this._maybeFetchChart(id,d);
  }

  _fmt(v){return v===undefined||v===null?"—":Number(v).toFixed(0);}
  _timeLeft(ts){const s=Math.max(0,Math.round(Number(ts)-Date.now()/1000));if(s<60)return `${s}s`;if(s<3600)return `${Math.ceil(s/60)}m`;return `${Math.ceil(s/3600)}h`;}
  _esc(v){return String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
}
customElements.define("ld2410-tuner-panel",LD2410TunerPanel);
