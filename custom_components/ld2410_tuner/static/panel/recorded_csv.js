// Text exports retain stored values; they never reuse graph buckets.
export function rawTimestamp(value) {
  return new Date(Math.round(value * 1000)).toISOString();
}

export function rawCsvPage(page, header = false) {
  const cell = (value) => {
    const text = value == null ? "" : String(value);
    return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
  };
  const rows = page.rows.map(([start, end, ...values]) => [
    rawTimestamp(start),
    rawTimestamp(end),
    ((Math.round(end * 1000) - Math.round(start * 1000)) / 1000).toFixed(3),
    ...values,
  ]);
  if (header)
    rows.unshift(["start_utc", "end_utc", "duration_seconds", ...page.keys]);
  return rows.map((row) => row.map(cell).join(",")).join("\r\n") + "\r\n";
}
