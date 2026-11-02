import { CHART_H, CHART_MARGIN } from "./constants.js";

const KINDS = {
  manual: "Human (manual)",
  entity: "Entity / sensor",
  guessed: "Guessed (automatic)",
};
const STATES = {
  present: ["Present", "#2e7d32", ""],
  not_present: ["Not present", "#777", "5 2"],
  unknown: ["Unknown", "#b58900", "1 2"],
};
const STORAGE = "ld2410_tuner.presence-lanes";

// Clip before sorting; coalesce contiguous runs and batch each state into one path.
export function lanePaths(labels, bounds, width, y) {
  const paths = Object.fromEntries(
    Object.keys(STATES).map((state) => [state, []]),
  );
  const clipped = labels
    .filter(
      (l) =>
        STATES[l.state] &&
        Number.isFinite(l.start) &&
        Number.isFinite(l.end) &&
        l.end > bounds.start &&
        l.start < bounds.end,
    )
    .map((l) => ({
      state: l.state,
      start: Math.max(bounds.start, l.start),
      end: Math.min(bounds.end, l.end),
    }))
    .sort((a, b) => a.start - b.start);
  const merged = [];
  for (const label of clipped) {
    const last = merged.at(-1);
    if (last && last.state === label.state && label.start <= last.end)
      last.end = Math.max(last.end, label.end);
    else merged.push(label);
  }
  const scale = (t) =>
    CHART_MARGIN.left +
    ((t - bounds.start) / (bounds.end - bounds.start)) *
      (width - CHART_MARGIN.left - CHART_MARGIN.right);
  for (const l of merged)
    if (l.end > l.start)
      paths[l.state].push(
        `M${scale(l.start).toFixed(2)},${y}H${scale(l.end).toFixed(2)}`,
      );
  return Object.entries(paths)
    .filter(([, parts]) => parts.length)
    .map(([state, parts]) => {
      const [name, color, dash] = STATES[state];
      return `<path data-presence-state="${state}" d="${parts.join("")}" fill="none" stroke="${color}" stroke-width="8" stroke-dasharray="${dash}"><title>${name}</title></path>`;
    })
    .join("");
}

export const panelPresenceLanes = {
  _presenceVisibility() {
    if (!this._presenceShown) {
      let saved;
      try {
        saved = JSON.parse(localStorage.getItem(STORAGE));
      } catch {
        /* Private browsing or invalid preferences. */
      }
      this._presenceShown = Object.fromEntries(
        Object.keys(KINDS).map((kind) => [kind, saved?.[kind] !== false]),
      );
    }
    return this._presenceShown;
  },

  _presenceLanesSvg(evidence, bounds, width) {
    const shown = this._presenceVisibility();
    const rows = Object.entries(KINDS).flatMap(([kind, name]) =>
      kind === "guessed" && evidence?.guessedMembers?.length
        ? evidence.guessedMembers.map((member) => [
            kind,
            `Guessed - ${this._esc(member.name)}`,
            member.labels,
          ])
        : [[kind, name, evidence?.[kind] || []]],
    );
    return rows
      .map(([kind, name, labels], i) => {
        const y = CHART_H + i * 32;
        const paths = lanePaths(labels, bounds, width, y + 21);
        return `<g data-presence-lane="${kind}" style="display:${shown[kind] ? "inline" : "none"}"><text x="${CHART_MARGIN.left}" y="${y + 10}" class="axis-label">${name}${paths ? "" : " · no evidence in window"}</text>${paths}</g>`;
      })
      .join("");
  },

  _presenceLegendHtml() {
    const shown = this._presenceVisibility();
    return `<div class="presence-evidence"><div class="presence-legend" role="group" aria-label="Show presence evidence" style="display:flex;flex-wrap:wrap;gap:6px;margin:6px 0">${Object.entries(
      KINDS,
    )
      .map(
        ([kind, name]) =>
          `<button type="button" data-presence-toggle="${kind}" aria-pressed="${shown[kind]}">${name}: ${shown[kind] ? "Shown" : "Hidden"}</button>`,
      )
      .join(
        "",
      )}</div><p class="muted">Presence evidence: solid = Present; dashed = Not present; dotted = Unknown. Gaps mean no evidence. Automatic evidence is not confirmed truth.</p></div>`;
  },

  _wirePresenceLanes(root) {
    for (const button of root.querySelectorAll("[data-presence-toggle]"))
      button.onclick = () => {
        const shown = this._presenceVisibility();
        const kind = button.dataset.presenceToggle;
        shown[kind] = !shown[kind];
        try {
          localStorage.setItem(STORAGE, JSON.stringify(shown));
        } catch {
          /* Still works without storage. */
        }
        for (const lane of this.shadowRoot.querySelectorAll(
          `[data-presence-lane="${kind}"]`,
        ))
          lane.style.display = shown[kind] ? "inline" : "none";
        for (const copy of this.shadowRoot.querySelectorAll(
          `[data-presence-toggle="${kind}"]`,
        )) {
          copy.setAttribute("aria-pressed", String(shown[kind]));
          copy.textContent = `${KINDS[kind]}: ${shown[kind] ? "Shown" : "Hidden"}`;
        }
      };
  },

  _roomPresenceEvidence(gid, data) {
    const group = this._roomsData().groups[gid] || {};
    const labels = group.labels || [];
    return {
      manual: labels.filter((l) => l.source === "manual"),
      entity: labels.filter((l) => l.source !== "manual"),
      guessed: [],
      guessedMembers: (data?.series || []).map((item) => ({
        name: item.name,
        labels: item.chart.presence_evidence?.guessed || [],
      })),
    };
  },
};
