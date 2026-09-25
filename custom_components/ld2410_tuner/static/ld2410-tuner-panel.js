import { panelTimeline } from "./panel/timeline.js";
import { panelActivity } from "./panel/activity.js";
import { panelVisualization } from "./panel/visualization.js";
import { panelCards } from "./panel/card.js";
import { panelControls } from "./panel/controls.js";
import { panelLearning } from "./panel/learning.js";
import { panelChart } from "./panel/chart.js";
import { panelSelection } from "./panel/selection.js";
import { panelCalendar } from "./panel/calendar.js";
import { panelHistory } from "./panel/history.js";
import { panelView } from "./panel/view.js";

// One distinct color per gate (0-8), shared between "move" and "still" views
// so a gate keeps its identity when you switch kind.

// Which card subsections start collapsed the first time a device is drawn.
// Training + the chart are the two things you actually need to glance at
// while tuning, so those two start open; everything else is one tap away.

class LD2410TunerPanel extends HTMLElement {
  constructor() {
    super();
    // Capture the version of this loaded entrypoint; polling must not relabel an old tab.
    this._frontendVersion = new URL(import.meta.url).searchParams.get("v");
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
    this._historyState = new Map();
    this._chartQueue = [];
    this._activeCharts = 0;
    this._activeActions = 0;
    this._busyCards = new Set();
    this._actionError = "";
    this._onVisibilityChange = () => {
      if (!document.hidden) this._load();
    };
  }

  set hass(hass) {
    this._hass = hass;
    if (!this.shadowRoot) this._render();
    if (!this._loaded) this._load();
    if (!this._pollTimer) {
      // Skip polling while the tab/panel isn't visible - no point hitting
      // the websocket every 3s for a view nobody's looking at - and catch
      // up immediately when it becomes visible again.
      this._pollTimer = setInterval(() => {
        if (!document.hidden) this._load();
      }, 3000);
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
    if (this._dragging || this._activeActions) return true;
    const active = this.shadowRoot?.activeElement;
    if (!active) return false;
    return ["INPUT", "SELECT", "TEXTAREA"].includes(active.tagName);
  }

  async _export(id, format) {
    const data = await this._call("export", { device_id: id });
    if (format === "csv")
      this._download(this._csv(data), `ld2410-tuner-${id}.csv`, "text/csv");
    else
      this._download(
        JSON.stringify(data, null, 2),
        `ld2410-tuner-${id}.json`,
        "application/json",
      );
  }

  _download(content, filename, type) {
    const blob = new Blob([content], { type });
    const file = new File([blob], filename, { type });
    if (navigator.share && navigator.canShare?.({ files: [file] })) {
      navigator
        .share({ title: "LD2410 Tuner export", files: [file] })
        .catch(() => {});
      return;
    }
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  _csv(data) {
    const rows = [
      [
        "device",
        "gate",
        "kind",
        "present_samples",
        "not_present_samples",
        "present_p50",
        "present_p95",
        "present_p99",
        "present_max",
        "not_present_p50",
        "not_present_p95",
        "not_present_p99",
        "not_present_max",
        "current_threshold",
        "learned_threshold",
        "sensitivity",
        "specificity",
        "false_positive_rate",
        "false_negative_rate",
        "noise_floor_p99",
        "noise_ceiling",
        "safety_margin",
        "status",
        "auto_used",
        "auto_present_samples",
        "auto_not_present_samples",
      ],
    ];
    for (const d of Object.values(data.devices || {})) {
      for (let g = 0; g < 9; g++)
        for (const kind of ["move", "still"]) {
          const key = `g${g}_${kind}`,
            ps = d.histogram_stats?.[key]?.present || {},
            ns = d.histogram_stats?.[key]?.not_present || {};
          const p = d.last_learning?.proposals?.[key] || {};
          rows.push([
            d.name,
            `G${g}`,
            kind,
            ps.count || 0,
            ns.count || 0,
            ps.p50 ?? "",
            ps.p95 ?? "",
            ps.p99 ?? "",
            ps.max ?? "",
            ns.p50 ?? "",
            ns.p95 ?? "",
            ns.p99 ?? "",
            ns.max ?? "",
            d.current_thresholds?.[key] ?? "",
            p.threshold ?? "",
            p.sensitivity ?? "",
            p.specificity ?? "",
            p.false_positive_rate ?? "",
            p.false_negative_rate ?? "",
            p.noise_floor_p99 ?? "",
            p.noise_ceiling ?? "",
            p.safety_margin ?? "",
            p.status || "",
            p.auto_used ? "yes" : "no",
            p.auto_samples?.present ?? 0,
            p.auto_samples?.not_present ?? 0,
          ]);
        }
      rows.push(
        [],
        ["AUTO SEGMENTS", "state", "timestamp", "confidence", "score"],
      );
      for (const seg of d.auto_learning?.segments || []) {
        rows.push([
          d.name,
          seg.state,
          new Date(Number(seg.start) * 1000).toISOString(),
          seg.confidence ?? "",
          seg.score ?? "",
        ]);
      }
    }
    return rows
      .map((r) =>
        r.map((v) => `"${String(v ?? "").replaceAll('"', '""')}"`).join(","),
      )
      .join("\n");
  }

  // Two independently draggable handles mark a time range; a small toolbar
  // below the chart lets you cancel, or open a panel with editable
  // start/end fields (kept in sync with the handles both ways) and buttons
  // to commit PRESENT / NOT PRESENT / UNKNOWN for that range.
}
Object.assign(
  LD2410TunerPanel.prototype,
  panelTimeline,
  panelActivity,
  panelChart,
  panelVisualization,
  panelSelection,
  panelView,
  panelCards,
  panelControls,
  panelLearning,
  panelHistory,
  panelCalendar,
);
customElements.define("ld2410-tuner-panel", LD2410TunerPanel);
