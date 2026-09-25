export const CHART_W = 760,
  CHART_H = 220;

export const CHART_MARGIN = { left: 34, right: 10, top: 10, bottom: 24 };

export const CHART_PLOT_W = CHART_W - CHART_MARGIN.left - CHART_MARGIN.right;

export const CHART_PLOT_H = CHART_H - CHART_MARGIN.top - CHART_MARGIN.bottom;

export const CHART_RANGES = [
  { label: "1 hour", hours: 1 },
  { label: "6 hours", hours: 6 },
  { label: "24 hours", hours: 24 },
  { label: "3 days", hours: 72 },
  { label: "7 days", hours: 168 },
  { label: "30 days", hours: 720 },
];

export const GATE_COLORS = [
  "#e53935",
  "#fb8c00",
  "#c0a000",
  "#43a047",
  "#00897b",
  "#1e88e5",
  "#5e35b1",
  "#8e24aa",
  "#6d4c41",
];

export const SECTION_DEFAULTS = {
  training: false,
  chart: false,
  history: true,
  auto: true,
  details: false,
  actions: true,
};
