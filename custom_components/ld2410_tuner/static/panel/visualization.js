import {
  CHART_W,
  CHART_H,
  CHART_MARGIN,
  CHART_PLOT_W,
  CHART_PLOT_H,
  GATE_COLORS,
} from "./constants.js";

export const panelVisualization = {
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
    const learnedThreshold = learnedProposal?.threshold ?? null;
    const learnedUnsafe = learnedProposal && learnedProposal.status !== "ok";
    // The single highest NOT_PRESENT sample ever seen for this gate - shown
    // so an isolated spike dominating the learned threshold is visible at a
    // glance, rather than being an invisible number behind the math.
    const noiseCeiling = learnedProposal?.noise_ceiling ?? null;

    const span = Math.max(1, data.end - data.start);
    const xScale = (t) =>
      CHART_MARGIN.left + ((t - data.start) / span) * CHART_PLOT_W;
    const yScale = (v) =>
      CHART_MARGIN.top +
      (1 - Math.max(0, Math.min(100, v)) / 100) * CHART_PLOT_H;
    const thresholdLine = (value, style, label) => {
      if (value == null || !Number.isFinite(Number(value))) return "";
      const y = yScale(Number(value));
      return `<line x1="${CHART_MARGIN.left}" y1="${y}" x2="${CHART_W - CHART_MARGIN.right}" y2="${y}" class="threshold-line ${style}"></line><text x="${CHART_MARGIN.left + 4}" y="${Math.max(9, y - 3)}" class="axis-label ${style}">${this._esc(label)} ${this._fmt(value)}</text>`;
    };

    const bandColor = {
      present: "rgba(46,125,50,0.18)",
      not_present: "rgba(120,120,120,0.14)",
      unknown: "rgba(255,193,7,0.14)",
    };
    const bands = (data.labels || [])
      .map((l) => {
        const x0 = xScale(Math.max(l.start, data.start));
        const x1 = xScale(Math.min(l.end, data.end));
        const fill = bandColor[l.state];
        if (!fill || x1 <= x0) return "";
        return `<rect x="${x0.toFixed(1)}" y="${CHART_MARGIN.top}" width="${(x1 - x0).toFixed(1)}" height="${CHART_PLOT_H}" fill="${fill}"></rect>`;
      })
      .join("");

    const yTicks = [0, 25, 50, 75, 100]
      .map((v) => {
        const y = yScale(v);
        return `<line x1="${CHART_MARGIN.left}" y1="${y.toFixed(1)}" x2="${CHART_W - CHART_MARGIN.right}" y2="${y.toFixed(1)}" class="grid-line"></line><text x="${CHART_MARGIN.left - 6}" y="${(y + 3).toFixed(1)}" class="axis-label" text-anchor="end">${v}</text>`;
      })
      .join("");

    const spanHours = span / 3600;
    const tickCount = 5;
    const timeFmt = (t) => {
      const dt = new Date(t * 1000);
      return dt.toLocaleString(undefined, {
        ...(spanHours >= 12 ? { month: "short", day: "numeric" } : {}),
        hour: "2-digit",
        minute: "2-digit",
      });
    };
    const xTicks = Array.from({ length: tickCount }, (_, i) => {
      const t = data.start + (i / (tickCount - 1)) * span;
      const x = xScale(t);
      return `<line x1="${x.toFixed(1)}" y1="${CHART_MARGIN.top}" x2="${x.toFixed(1)}" y2="${CHART_H - CHART_MARGIN.bottom}" class="grid-line"></line><text x="${x.toFixed(1)}" y="${CHART_H - CHART_MARGIN.bottom + 16}" class="axis-label" text-anchor="${this._tickAnchor(i, tickCount)}">${this._esc(timeFmt(t))}</text>`;
    }).join("");

    // Every non-highlighted gate of this kind gets a thin, muted line for
    // silhouette/comparison; the highlighted gate gets the full treatment
    // (shaded min-max band, bold line).
    const otherLines = this._otherGateLines(data, cs, xScale, yScale);
    const { activeBandSvg, activeLineSvg } = this._activeGatePaths(
      data,
      cs,
      activeKey,
      xScale,
      yScale,
    );
    const selectionSvg = this._selectionSvg(cs, xScale);

    const toolbarHtml = this._selectionToolbarHtml(cs);

    return `${this._learnStatusHtml(learnedProposal, cs)}<svg viewBox="0 0 ${CHART_W} ${CHART_H}" preserveAspectRatio="xMidYMid meet">
      <rect x="${CHART_MARGIN.left}" y="${CHART_MARGIN.top}" width="${CHART_PLOT_W}" height="${CHART_PLOT_H}" class="plot-bg"></rect>
      ${bands}
      ${yTicks}
      ${xTicks}
      ${otherLines}
      ${activeBandSvg}
      ${activeLineSvg}
      ${thresholdLine(learnedThreshold, learnedUnsafe ? "learned-unsafe" : "learned", learnedUnsafe ? "Learned (unsafe)" : "Learned")}
      ${thresholdLine(currentThreshold, "current", "Current")}
      ${thresholdLine(noiseCeiling, "noise-ceiling", "Noise max")}
      <rect data-role="selection-hit" class="selection-hit" x="${CHART_MARGIN.left}" y="${CHART_MARGIN.top}" width="${CHART_PLOT_W}" height="${CHART_PLOT_H}"></rect>
      ${selectionSvg}
    </svg>${toolbarHtml}`;
  },
  _tickAnchor(index, count) {
    if (index === 0) return "start";
    return index === count - 1 ? "end" : "middle";
  },

  _seriesSegments(points, bucketSeconds) {
    const groups = [];
    for (const point of points) {
      const last = groups.at(-1);
      if (
        !last ||
        (bucketSeconds && point.t - last.at(-1).t > bucketSeconds * 2)
      )
        groups.push([point]);
      else last.push(point);
    }
    return groups;
  },

  _otherGateLines(data, cs, xScale, yScale) {
    let otherLines = "";
    for (let g = 0; g < 9; g++) {
      if (g === cs.gate) continue;
      const s = data.series?.[`g${g}_${cs.kind}`];
      if (!s?.points.length) continue;
      const path = this._seriesSegments(s.points, data.bucket_seconds)
        .map((points) =>
          points
            .map(
              (p, i) =>
                `${i === 0 ? "M" : "L"}${xScale(p.t).toFixed(1)},${yScale(p.avg).toFixed(1)}`,
            )
            .join(" "),
        )
        .join(" ");
      otherLines += `<path d="${path}" class="gate-line" style="stroke:${GATE_COLORS[g]}"></path>`;
    }

    return otherLines;
  },

  _activeGatePaths(data, cs, activeKey, xScale, yScale) {
    const activeSeries = data.series?.[activeKey];
    let activeBandSvg = "",
      activeLineSvg = "";
    if (activeSeries?.points.length) {
      for (const points of this._seriesSegments(
        activeSeries.points,
        data.bucket_seconds,
      )) {
        const linePath = points
          .map(
            (p, i) =>
              `${i === 0 ? "M" : "L"}${xScale(p.t).toFixed(1)},${yScale(p.avg).toFixed(1)}`,
          )
          .join(" ");
        const bandTop = points
          .map((p) => `${xScale(p.t).toFixed(1)},${yScale(p.max).toFixed(1)}`)
          .join(" L ");
        const bandBottom = points
          .slice()
          .reverse()
          .map((p) => `${xScale(p.t).toFixed(1)},${yScale(p.min).toFixed(1)}`)
          .join(" L ");
        activeBandSvg += `<path d="M ${bandTop} L ${bandBottom} Z" class="value-band" style="fill:${GATE_COLORS[cs.gate]}"></path>`;
        activeLineSvg += `<path d="${linePath}" class="value-line active" style="stroke:${GATE_COLORS[cs.gate]}"></path>`;
        if (points.length === 1)
          activeLineSvg += `<circle cx="${xScale(points[0].t)}" cy="${yScale(points[0].avg)}" r="2" fill="${GATE_COLORS[cs.gate]}"></circle>`;
      }
    }

    return { activeBandSvg, activeLineSvg };
  },

  _selectionSvg(cs, xScale) {
    let selectionSvg = `<rect data-role="selection-rect" class="selection-rect" x="0" y="${CHART_MARGIN.top}" width="0" height="${CHART_PLOT_H}" style="display:none"></rect>`;
    if (cs.selection) {
      const xStart = xScale(cs.selection.start),
        xEnd = xScale(cs.selection.end);
      const x0 = Math.min(xStart, xEnd),
        x1 = Math.max(xStart, xEnd);
      const handle = (
        role,
        x,
      ) => `<g data-role="handle-${role}" class="range-handle">
          <line x1="${x.toFixed(1)}" y1="${CHART_MARGIN.top}" x2="${x.toFixed(1)}" y2="${CHART_H - CHART_MARGIN.bottom}"></line>
          <circle cx="${x.toFixed(1)}" cy="${(CHART_MARGIN.top + CHART_PLOT_H / 2).toFixed(1)}" r="7"></circle>
          <rect class="handle-hit" x="${(x - 11).toFixed(1)}" y="${CHART_MARGIN.top}" width="22" height="${CHART_PLOT_H}"></rect>
        </g>`;
      selectionSvg = `<rect data-role="selection-rect" class="selection-rect" x="${x0.toFixed(1)}" y="${CHART_MARGIN.top}" width="${(x1 - x0).toFixed(1)}" height="${CHART_PLOT_H}"></rect>
        ${handle("start", xStart)}
        ${handle("end", xEnd)}`;
    }

    return selectionSvg;
  },
};
