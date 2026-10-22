// Shared containment and rhythm for the room → zone → radar hierarchy.
export const CARD_STYLES = `
.section-description { display:block; margin-top:3px; font-size:12px; font-weight:400; color:var(--secondary-text-color); line-height:1.4; }
:host { padding:16px; font-family:var(--paper-font-body1_-_font-family, var(--ha-font-family-body, Roboto, system-ui, sans-serif)); font-size:14px; line-height:1.5; }
h2, h3 { font-size:16px; }
.subsection-head { width:100%; min-height:44px; text-align:start; border:0; border-radius:0; }
.action-status:empty, .learning-job-status:empty, #snapshot-status:empty { display:none; }
*, *::before, *::after { box-sizing:border-box; }
.card, .panel-card { min-width:0; max-width:100%; border:1px solid var(--divider-color,#ddd); border-radius:14px; padding:16px; box-shadow:var(--ha-card-box-shadow,0 1px 3px rgba(0,0,0,.06)); overflow-wrap:anywhere; }
.layout-node, .layout-leaf { margin:0; width:100%; align-self:stretch; }
.layout-node > summary, .layout-leaf > summary { padding:0 0 8px; }
.layout-node > .layout-leaf { margin-top:16px; }
.layout-node > .group-header, .layout-leaf > .group-header { margin:8px 0 14px; background:var(--secondary-background-color,#f7f8fa); box-shadow:none; }
.layout-node > .layout-leaf, .layout-leaf > .grid, .layout-direct > .grid { width:100%; min-width:0; }
.group-summary { min-height:44px; font-size:14px; }
.group-context { margin-inline-start:10px; color:var(--secondary-text-color); font-size:12px; }
.group-score { display:block; margin-top:4px; color:var(--secondary-text-color); font-size:12px; }
.room-labels { margin:0; padding-top:12px; border-top:0; }
.room-labels > p:first-of-type { margin:4px 0 12px; }
.room-settings { border-top:1px solid var(--divider-color); margin-top:16px; padding-top:8px; }
.room-settings > summary, .inline-help > summary { cursor:pointer; min-height:44px; align-content:center; }
.room-graph { min-width:0; margin:12px 0; }
.field-note { margin:6px 0; font-size:13px; color:var(--secondary-text-color); }
.field-note[hidden] { display:none; }
.room-signal-canvas { min-width:0; width:100%; overflow:hidden; border:1px solid var(--divider-color); border-radius:10px; background:var(--card-background-color,#fff); }
.room-signal-canvas svg { display:block; width:100%; height:auto; min-height:160px; touch-action:none; }
.room-signal-canvas > p { padding:16px; }
.room-label-band.present { fill:rgba(46,125,50,.13); }
.room-label-band.not_present { fill:rgba(120,144,156,.13); }
.room-label-band.unknown { fill:rgba(255,193,7,.13); }
.room-signal-canvas [hidden] { display:none; }
.room-signal-canvas .axis-label { font-size:12px; }
.chart-canvas .axis-label { font-size:12px; }
.room-graph-status { min-height:3em; margin:6px 0; font-size:12px; }
.room-graph-legend { display:flex; flex-wrap:wrap; gap:8px; margin:10px 0; }
.room-graph-legend button { display:flex; flex-wrap:wrap; align-items:center; gap:6px; min-width:0; max-width:100%; text-align:start; white-space:normal; }
.room-graph-legend svg { width:24px; height:10px; flex:none; }
.room-graph-legend line { stroke:currentColor; stroke-width:2; }
.room-graph-legend button span { color:var(--secondary-text-color); font-size:12px; }
.card :is(input:not([type="checkbox"]):not([type="radio"]), select), .panel-card :is(input:not([type="checkbox"]):not([type="radio"]), select) { min-width:0; max-width:100%; font:inherit; min-height:44px; border:1px solid var(--divider-color); border-radius:8px; background:var(--card-background-color,#fff); color:var(--primary-text-color); padding:8px; }
.card :is(label,form,section,details,fieldset,nav), .panel-card :is(label,form,section,details,fieldset,nav) { min-width:0; max-width:100%; }
.card button, .panel-card button { max-width:100%; overflow-wrap:anywhere; }
.card h2, .card h3, .panel-card h2, .panel-card h3 { line-height:1.3; }
.subsection-head:focus-visible, .top:focus-visible { outline:2px solid var(--primary-color); outline-offset:-2px; }
.history-fields { grid-template-columns:repeat(2,minmax(0,1fr)) minmax(100px,.7fr); }
.inline-help { grid-column:1 / -1; font-size:13px; color:var(--secondary-text-color); }
.learning-schedule { display:grid; grid-template-columns:repeat(auto-fit,minmax(min(240px,100%),1fr)); align-items:end; gap:12px; }
.learning-schedule > label:not(:has(input[type="checkbox"])) { flex-wrap:wrap; min-width:0; }
.learning-schedule > [role="status"] { grid-column:1 / -1; }
@media (max-width:700px) {
  :host { padding:8px; }
  .card, .panel-card { padding:12px; border-radius:12px; }
  .layout-node, .layout-leaf { padding:10px; }
  .group-header.panel-card { padding:10px; }
  .grid { grid-template-columns:minmax(0,1fr); gap:12px; }
  .history-fields { grid-template-columns:minmax(0,1fr); }
  .history-navigation label { min-width:0; flex:1 1 100%; order:-1; }
  .history-navigation button { flex:1; }
  .room-graph .chart-controls { display:grid; grid-template-columns:minmax(0,1fr); }
  .room-graph .chart-controls label, .room-graph .chart-controls select { display:block; width:100%; }
  .room-graph .chart-controls button { width:100%; }
  .group-context { display:block; margin-inline-start:0; }
  .history-buttons button { flex:1 1 100%; }
  .history-buttons { display:flex; flex-wrap:wrap; gap:8px; }
}
`;
