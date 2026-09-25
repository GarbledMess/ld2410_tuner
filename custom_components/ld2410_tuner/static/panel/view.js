import { SECTION_DEFAULTS } from "./constants.js";

export const panelView = {
  _render() {
    this.attachShadow({ mode: "open" });
    this.shadowRoot.innerHTML = `
      <style>
        :host { display:block; padding:12px; box-sizing:border-box; color:var(--primary-text-color); }
        .wrap { max-width:1500px; margin:auto; }
        h1 { font-size:26px; margin:4px 0 6px; display:flex; align-items:center; flex-wrap:wrap; gap:8px; }
        .panel-version { font-size:12px; font-weight:400; color:var(--secondary-text-color); border:1px solid var(--divider-color); border-radius:6px; padding:3px 6px; }
        .action-status, #snapshot-status { min-height:1.5em; font-size:13px; margin:6px 0; }
        .is-busy::before { content:""; display:inline-block; width:12px; height:12px; border:2px solid var(--divider-color,#ccc); border-top-color:var(--primary-color,#1976d2); border-radius:50%; margin-right:7px; vertical-align:middle; animation:busy-spin .8s linear infinite; }
        button[aria-busy="true"] { position:relative; color:transparent; }
        button[aria-busy="true"]::after { content:""; position:absolute; left:calc(50% - 8px); top:calc(50% - 8px); width:12px; height:12px; border:2px solid var(--divider-color,#ccc); border-top-color:var(--primary-text-color,#222); border-radius:50%; animation:busy-spin .8s linear infinite; }
        @keyframes busy-spin { to { transform:rotate(360deg); } }
        @media (prefers-reduced-motion:reduce) { .is-busy::before, button[aria-busy="true"]::after { animation:none; } }
        .gate-results { margin-top:12px; }
        .gate-results summary { padding:10px 0; cursor:pointer; font-weight:600; }
        .data-clear { border-top:1px solid var(--divider-color); padding-top:12px; margin-top:12px; display:flex; flex-wrap:wrap; gap:10px; align-items:center; }
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
        .history-calendar { margin-top:12px; }
        .calendar-heading { display:flex; align-items:center; justify-content:space-between; gap:8px; margin-bottom:8px; }
        .calendar-grid { display:grid; grid-template-columns:repeat(7,minmax(0,1fr)); gap:3px; margin-bottom:8px; }
        .calendar-weekday { font-size:11px; text-align:center; color:var(--secondary-text-color); }
        .calendar-day { display:flex; flex-direction:column; justify-content:center; align-items:center; min-height:44px; padding:4px 0; font-size:13px; }
        .calendar-day.other-month { color:var(--secondary-text-color); border-color:transparent; }
        .calendar-day.today { border-color:var(--primary-color); }
        .calendar-day[aria-pressed="true"] { background:var(--secondary-background-color); outline:2px solid var(--primary-color); outline-offset:-2px; font-weight:700; }
        .calendar-dots { display:flex; gap:2px; height:6px; margin-top:3px; }
        .calendar-dots i { width:5px; height:5px; border-radius:50%; }
        .history-navigation { display:flex; flex-wrap:wrap; align-items:flex-end; gap:8px; margin:12px 0; }
        .history-navigation label { flex:1; min-width:130px; font-size:12px; }
        .history-navigation input { display:block; width:100%; min-width:0; min-height:44px; box-sizing:border-box; padding:8px; font:inherit; border:1px solid var(--divider-color); border-radius:9px; background:var(--card-background-color); color:var(--primary-text-color); }
        .history-range { position:relative; margin:12px 0; touch-action:none; user-select:none; -webkit-user-select:none; cursor:crosshair; }
        .history-range .history-timeline { height:44px; }
        .history-range-selection { position:absolute; top:0; bottom:0; background:rgba(30,136,229,.25); border-inline:2px solid #1565c0; box-sizing:border-box; pointer-events:none; }
        .history-range-handle { position:absolute; top:0; bottom:0; left:clamp(0px,calc(var(--position) - 22px),calc(100% - 44px)); width:44px; padding:0; border:0; background:transparent; cursor:ew-resize; touch-action:none; }
        .history-range-handle::after { content:""; display:block; margin:auto; height:24px; width:8px; background:#1565c0; border:2px solid #fff; border-radius:5px; }
        .history-range [hidden] { display:none; }
        .history-timeline { display:flex; height:28px; border:1px solid var(--divider-color); border-radius:6px; overflow:hidden; }
        .history-segment { display:block; height:100%; }
        .history-segment.present { background:#43a047; }
        .history-segment.not_present { background:#78909c; }
        .history-segment.unknown { background:#ffc107; }
        .history-segment.unlabelled { background:repeating-linear-gradient(135deg,transparent,transparent 4px,var(--divider-color) 4px,var(--divider-color) 6px); }
        .history-axis { display:flex; justify-content:space-between; gap:8px; font-size:11px; margin:4px 0 8px; }
        .history-legend { display:flex; flex-wrap:wrap; gap:8px; font-size:12px; }
        .history-legend span { display:flex; align-items:center; gap:4px; }
        .history-legend i { width:12px; height:12px; border:1px solid var(--divider-color); }
        .history-periods { display:grid; gap:6px; max-height:300px; overflow:auto; margin:12px 0; }
        .history-period { display:flex; flex-wrap:wrap; gap:8px; justify-content:space-between; text-align:left; font-size:12px; width:100%; min-height:48px; }
        .history-status.present { color:var(--state-active-color,#2e7d32); }
        .history-edit-note { font-size:13px; margin:12px 0 0; }
        .history-buttons { display:flex; flex-wrap:wrap; gap:8px; }
        .history-fields label { min-width:0; }
        .history-buttons [hidden] { display:none; }
        .stats { display:grid; grid-template-columns:repeat(2,1fr); gap:8px; margin-bottom:12px; }
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
        .chart-canvas svg { display:block; width:100%; height:auto; touch-action:none; user-select:none; -webkit-user-select:none; }
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
          .stats { grid-template-columns:1fr 1fr; gap:5px; }
          .stat { padding:8px 6px; font-size:11px; }
          .stat b { font-size:15px; }
          .table-scroll { display:none; }
          .mobile-gates { display:block; }
          .chart-controls select { flex:1 1 45%; min-width:0; }
        }
      </style>
      <div class="wrap">
        <div id="error" role="alert" class="notice warn" hidden></div>
        <h1>LD2410 Tuner <span class="panel-version" aria-label="Loaded panel version" title="Version requested when this panel loaded. Reload the page after updating.">${this._frontendVersion ? `v${this._esc(this._frontendVersion)}` : "Version unavailable"}</span></h1>
        <div class="subtitle">Automatic estimates learn from signal patterns over time and carry confidence scores. Add empty-room, moving and quiet-sitting examples to improve them. Learn prioritizes reliable presence across sessions; human labels always take priority over lower-confidence estimates. Inferred data proportions do not block Apply.</div>
        <div id="snapshot-status" class="muted" role="status" aria-live="polite"></div>
        <div class="grid" id="grid"></div>
      </div>`;
    // If a poll landed while a field was focused, it's queued instead of
    // applied (see _load). Once focus leaves the panel, catch up on that
    // queued redraw so things don't just go stale until the next poll.
    this.shadowRoot.addEventListener(
      "toggle",
      (event) => {
        const card = event.target.closest?.("[data-device-id]");
        if (card && event.target.matches(".gate-results"))
          this._sectionState.set(
            `${card.dataset.deviceId}:gate-table`,
            event.target.open,
          );
      },
      true,
    );
    this.shadowRoot.addEventListener("focusout", () => {
      setTimeout(() => {
        if (this._redrawPending && !this._isEditing()) {
          this._redrawPending = false;
          this._draw();
        }
      }, 0);
    });
  },

  _sectionCollapsed(id, key) {
    const k = `${id}:${key}`;
    if (!this._sectionState.has(k))
      this._sectionState.set(k, !!SECTION_DEFAULTS[key]);
    return this._sectionState.get(k);
  },

  _setSectionCollapsed(id, key, val) {
    this._sectionState.set(`${id}:${key}`, val);
  },

  _section(id, key, title, innerHtml) {
    const collapsed = this._sectionCollapsed(id, key);
    return `<div class="subsection${collapsed ? " collapsed" : ""}" data-section="${key}">
      <div class="subsection-head" data-action="section-toggle" data-section="${key}"><span>${this._esc(title)}</span><button class="sub-toggle" aria-expanded="${!collapsed}">${collapsed ? "▸" : "▾"}</button></div>
      <div class="subsection-body">${innerHtml}</div>
    </div>`;
  },

  _draw() {
    const grid = this.shadowRoot.querySelector("#grid");
    this._captureDrafts(grid);
    const charts = new Map(
      [...grid.querySelectorAll("[data-device-id]")].map((card) => [
        card.dataset.deviceId,
        card.querySelector('[data-section="chart"]'),
      ]),
    );
    const devices = this._data?.devices || {};
    if (!Object.keys(devices).length) {
      grid.innerHTML = `<div class="card">No LD2410 gate energy entities were discovered.</div>`;
      return;
    }
    const fragment = document.createDocumentFragment();
    for (const [id, device] of Object.entries(devices)) {
      fragment.appendChild(this._createCard(id, device, charts.get(id)));
    }
    grid.replaceChildren(fragment);
    for (const [id, device] of Object.entries(devices))
      this._maybeFetchChart(id, device);
  },

  _captureDrafts(grid) {
    for (const card of grid.querySelectorAll("[data-device-id]")) {
      const draft = {};
      card
        .querySelectorAll(
          '[data-action="history-start"], [data-action="history-end"], [data-action="history-state"], [data-action="timeout-hours"], [data-action="timeout-minutes"]',
        )
        .forEach((el) => {
          draft[el.dataset.action] = el.value;
        });
      this._drafts.set(card.dataset.deviceId, draft);
    }
  },

  _fmt(v) {
    return v === undefined || v === null ? "—" : Number(v).toFixed(0);
  },

  _timeLeft(ts) {
    const s = Math.max(0, Math.round(Number(ts) - Date.now() / 1000));
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.ceil(s / 60)}m`;
    return `${Math.ceil(s / 3600)}h`;
  },

  _esc(v) {
    return String(v ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
  },
};
