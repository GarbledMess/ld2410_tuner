const LANES = [
  ["calculations", "Main worker (including storage)"],
  ["display", "Display worker"],
  ["storage", "History save worker"],
];

const OPERATIONS = {
  learn: "Learn",
  comparison: "Comparison",
  room_assess: "Room assessment",
  room_fit: "Room fit",
  room_compare: "Room comparison",
  seen_by: "Presence attribution",
  references: "Reference update",
  recount: "Recount",
  charts: "Charts",
  recording_rows: "Recording rows",
  startup: "Startup",
  maintenance: "Storage maintenance",
  flush: "History save",
};

const STAGES = {
  preparing: "Preparing",
  starting: "Starting",
  running: "Running",
  finishing: "Finishing",
};

function duration(value) {
  if (!Number.isFinite(value)) return "—";
  const seconds = Math.max(0, Math.round(value));
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

export const panelWorkers = {
  _drawWorkers() {
    const slot = this.shadowRoot.querySelector("#workers");
    if (!slot) return;
    const workers = this._data?.workers;
    if (!workers) {
      this._updateHtml(
        slot,
        '<h2>Workers</h2><p class="muted">Status unavailable.</p>',
      );
      return;
    }
    const activeCount = LANES.filter(([key]) => workers[key]?.active).length;
    const queuedCount = LANES.reduce(
      (count, [key]) =>
        count +
        (workers[key]?.foreground || 0) +
        (workers[key]?.background || 0),
      0,
    );
    const rows = LANES.map(([key, label]) => {
      const lane = workers[key];
      const active = lane?.active;
      const queued = (lane?.foreground || 0) + (lane?.background || 0);
      const queue = queued ? `${queued} queued` : "No queue";
      if (!active)
        return `<div class="worker-row"><strong>${label}</strong><span>${lane?.closed ? "Unavailable" : "Idle"}</span><span class="muted">${queue}</span></div>`;
      const percent = active.progress_percent;
      const measured =
        Number.isFinite(percent) && percent >= 0 && percent <= 100;
      const eta =
        active.eta_state === "past_estimate"
          ? "Past estimate"
          : Number.isFinite(active.eta_seconds)
            ? `${duration(active.eta_seconds)} estimated remaining`
            : "Estimating…";
      const rss = active.resources?.rss_kib;
      const cpu = active.resources?.cpu_seconds;
      const resources = [
        Number.isFinite(rss) ? `${Math.round(rss / 1024)} MiB RSS` : null,
        Number.isFinite(cpu) ? `${duration(cpu)} CPU` : null,
      ]
        .filter(Boolean)
        .join(" · ");
      return `<div class="worker-row"><strong>${label}</strong><span>${this._esc(OPERATIONS[active.operation] || "Worker job")} · ${this._esc(STAGES[active.stage] || "Working")}</span><span>${duration(active.running_seconds)} elapsed · ${eta}</span><span class="muted">${queue}${resources ? ` · ${resources}` : ""}</span><div class="worker-progress"><progress aria-label="${label} progress" max="100"${measured ? ` value="${percent}"` : ""}></progress><span class="muted">${measured ? `${percent}%` : "Progress unavailable"}</span></div></div>`;
    }).join("");
    this._updateHtml(
      slot,
      `<h2>Workers <span class="muted">${activeCount} active · ${queuedCount} queued</span></h2><div class="worker-list">${rows}</div>`,
    );
  },
};
