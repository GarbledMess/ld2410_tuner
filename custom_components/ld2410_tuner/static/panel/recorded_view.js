import { rawTimestamp } from "./recorded_csv.js";

const bytes = (value = 0) =>
  value >= 1048576
    ? `${(value / 1048576).toFixed(2)} MiB`
    : `${(value / 1024).toFixed(2)} KiB`;
const disabled = (value) => (value ? "disabled" : "");

export const RECORDED_STYLES = `
  #recorded-data { font-size:14px; }
  #recorded-data > summary { cursor:pointer; min-height:44px; align-content:center; }
  #recorded-data > summary h2 { display:inline; font-size:16px; margin:0; }
  #recorded-data .raw-controls { display:flex; flex-wrap:wrap; align-items:end; gap:8px; margin:12px 0; }
  #recorded-data label { display:flex; flex-direction:column; gap:4px; min-width:0; }
  #recorded-data select { max-width:100%; }
  #recorded-data [data-raw-device] { width:280px; }
  #recorded-data .raw-days { display:flex; flex-wrap:wrap; gap:8px; margin:12px 0; max-height:240px; overflow:auto; }
  #recorded-data .raw-days button { text-align:left; }
  #recorded-data .raw-days button[aria-pressed="true"] { outline:2px solid var(--primary-color); outline-offset:-2px; }
  #recorded-data .raw-days small { display:block; color:var(--secondary-text-color); }
  #recorded-data .raw-table { max-width:100%; max-height:420px; overflow:auto; border:1px solid var(--divider-color); border-radius:8px; }
  #recorded-data th { position:sticky; top:0; background:var(--card-background-color); }
  #recorded-data td { font-variant-numeric:tabular-nums; }
  #recorded-data h3 { font-size:15px; margin:14px 0 8px; }
  #recorded-data p { line-height:1.5; }
`;

function dayList(s, esc) {
  if (!s.days) return "";
  const buttons = s.days.days
    .map(
      (d) =>
        `<button data-raw-open="${esc(d.date)}" aria-pressed="${d.date === s.date}" ${disabled(s.loading || s.export)}>${esc(d.date)} <small>${bytes(d.bytes)}${d.live ? " · Current day" : ""}</small></button>`,
    )
    .join("");
  return `<p class="muted">${s.days.total} recorded days · ${bytes(s.days.bytes)} for this radar. Days and timestamps use UTC.</p>
    <div class="raw-days">${buttons || "No recorded days for this radar."}</div>
    ${s.days.offset || s.days.next_offset !== null ? `<div class="raw-controls"><button data-raw-days="${Math.max(0, s.days.offset - 50)}" ${disabled(!s.days.offset || s.loading || s.export)}>Newer days</button><button data-raw-days="${s.days.next_offset}" ${disabled(s.days.next_offset === null || s.loading || s.export)}>Older days</button></div>` : ""}`;
}

function timeCell(value, date) {
  const stamp = rawTimestamp(value);
  const label =
    stamp.slice(0, 10) === date
      ? stamp.slice(11, 23)
      : stamp.slice(11, 23) + " (+1 day)";
  return `<td title="${stamp}">${label}</td>`;
}

function rawTable(s, esc) {
  const { page, kind } = s;
  const indices = Array.from({ length: 9 }, (_, gate) =>
    page.keys.indexOf(`g${gate}_${kind}`),
  );
  const headers = indices
    .map((_, gate) => `<th scope="col">G${gate}</th>`)
    .join("");
  const rows = page.rows
    .map(
      ([start, end, ...values]) =>
        `<tr>${timeCell(start, s.date)}${timeCell(end, s.date)}<td>${((Math.round(end * 1000) - Math.round(start * 1000)) / 1000).toFixed(3)}</td>${indices.map((i) => `<td>${values[i] == null ? '<span class="muted">Missing</span>' : esc(values[i])}</td>`).join("")}</tr>`,
    )
    .join("");
  return `<div class="raw-table" tabindex="0" role="region" aria-label="Raw gate readings; scroll for more columns"><table data-raw-values><thead><tr><th scope="col">Start (UTC)</th><th scope="col">End (UTC)</th><th scope="col">Seconds</th>${headers}</tr></thead><tbody>${rows}</tbody></table></div>`;
}

function readingView(s, esc) {
  if (!s.date) return "";
  const { page } = s;
  const count = page
    ? `${page.total ? page.offset + 1 : 0}–${page.offset + page.rows.length} of ${page.total} rows`
    : "";
  return `<section aria-label="Selected recording"><h3>${esc(s.date)} · Raw readings</h3>
    <div class="raw-controls"><label>Gate values<select data-raw-kind><option value="move" ${s.kind === "move" ? "selected" : ""}>Movement</option><option value="still" ${s.kind === "still" ? "selected" : ""}>Still</option></select></label>
    <button data-raw-refresh ${disabled(s.loading || s.export)}>Refresh readings</button><button data-raw-download ${disabled(!page || s.loading || s.export)}>Download day CSV</button>
    ${s.export ? "<button data-raw-cancel>Cancel export</button>" : ""}</div>
    <p class="muted">Exact stored gate energy (0–100), without averaging or additional rounding. Each row covers its start-to-end period; gaps are not filled. Missing means no value was recorded. CSV includes movement and still values for all nine gates.</p>
    ${page ? rawTable(s, esc) : ""}
    ${page ? `<div class="raw-controls"><button data-raw-prev ${disabled(!s.back.length || s.loading || s.export)}>Previous rows</button><span>${count}</span><button data-raw-next ${disabled(page.next_offset === null || s.loading || s.export)}>Next rows</button></div>${!page.total ? "<p>No stored rows for this day.</p>" : ""}` : ""}
    ${s.exportStatus ? `<p role="status">${esc(s.exportStatus)}</p>` : ""}</section>`;
}

export function recordedHtml(s, esc, devices = {}) {
  const options = s.devices
    .map(
      (d) =>
        `<option value="${esc(d.id)}" ${d.id === s.device ? "selected" : ""}>${esc(devices[d.id]?.name || d.name)}</option>`,
    )
    .join("");
  return `<div class="raw-controls">${options ? `<label>Radar<select data-raw-device ${disabled(s.export)}>${options}</select></label>` : ""}<button data-raw-inventory ${disabled(s.loading || s.export)}>Refresh days</button></div>
    ${s.storage ? `<p class="muted">Recorded history: ${bytes(s.storage.history_bytes ?? s.devices.reduce((sum, d) => sum + d.bytes, 0))}. Retained recovery files: ${bytes(s.storage.recovery_bytes)}. Sizes are actual disk bytes; recovery files are separate from the recordings below.</p>` : ""}
    ${s.loading ? '<p role="status" class="is-busy">Loading recorded data…</p>' : ""}
    ${s.error ? `<p role="alert" class="warn">${esc(s.error)}</p>` : ""}
    ${!s.loading && !s.error && s.started && !s.devices.length ? "<p>No recorded radars found.</p>" : ""}
    ${dayList(s, esc)}${readingView(s, esc)}`;
}
