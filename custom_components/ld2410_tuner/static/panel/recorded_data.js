import { rawCsvPage } from "./recorded_csv.js";
import { recordedHtml } from "./recorded_view.js";

export const panelRecordedData = {
  _recordedState(id) {
    this._recordedData ||= new Map();
    if (!this._recordedData.has(id))
      this._recordedData.set(id, {
        device: id,
        devices: [],
        kind: "move",
        request: 0,
        back: [],
      });
    return this._recordedData.get(id);
  },

  _recordedHtml(id) {
    return `<div class="raw-recordings" data-raw-content data-preserve-children>${recordedHtml(this._recordedState(id), (v) => this._esc(v))}</div>`;
  },

  _wireRecordedData(card, id) {
    const container = card.querySelector("[data-raw-content]");
    container.onclick = (event) => {
      const button = event.target.closest("button");
      if (button && !button.disabled) this._rawAction(id, button.dataset);
    };
    container.onchange = (event) => {
      if (event.target.matches("[data-raw-kind]")) {
        this._recordedState(id).kind = event.target.value;
        this._renderRecordedData(id);
      }
    };
  },

  _renderRecordedData(id) {
    const s = this._recordedState(id);
    for (const card of this._placements(id)) {
      const container = card.querySelector("[data-raw-content]");
      this._updateHtml(
        container,
        recordedHtml(s, (v) => this._esc(v)),
      );
    }
  },

  _rawAction(id, data) {
    const s = this._recordedState(id);
    if ("rawInventory" in data) void this._rawBootstrap(id);
    if ("rawOpen" in data) void this._rawRows(id, data.rawOpen);
    if ("rawRefresh" in data) void this._rawRows(id, s.date);
    if ("rawNext" in data)
      void this._rawRows(id, s.date, s.page.next_offset, "next");
    if ("rawPrev" in data)
      void this._rawRows(id, s.date, s.back.at(-1), "prev");
    if ("rawDays" in data) void this._rawDays(id, Number(data.rawDays));
    if ("rawDownload" in data) void this._rawDownload(id);
    if ("rawCancel" in data) {
      s.export.cancelled = true;
      s.exportStatus = "Cancelling export after the current page…";
      this._renderRecordedData(id);
    }
  },

  async _rawRequest(id, work) {
    const s = this._recordedState(id);
    const request = ++s.request;
    s.loading = true;
    s.error = null;
    this._renderRecordedData(id);
    const call = (type, parameters = {}) =>
      this._queueChartCall(async () => {
        if (request !== s.request || !this.isConnected)
          throw new Error("Read superseded");
        return this._call(type, parameters, 180000);
      });
    try {
      const accept = await work(call);
      if (request === s.request) accept();
    } catch (error) {
      if (request === s.request) s.error = error?.message || String(error);
    } finally {
      if (request === s.request) {
        s.loading = false;
        this._renderRecordedData(id);
      }
    }
  },

  async _rawBootstrap(id) {
    const s = this._recordedState(id);
    s.started = true;
    await this._rawRequest(id, async (call) => {
      const inventory = await call("recording_days");
      const device = id;
      const days = device
        ? await call("recording_days", { device_id: device })
        : null;
      return () => {
        Object.assign(s, {
          devices: inventory.devices,
          storage: inventory.storage,
          device,
          days,
          page: null,
          date: null,
          back: [],
        });
      };
    });
  },

  async _rawDays(id, offset = 0) {
    const s = this._recordedState(id);
    Object.assign(s, {
      device: id,
      days: null,
      page: null,
      date: null,
      back: [],
      exportStatus: null,
    });
    await this._rawRequest(id, async (call) => {
      const days = await call("recording_days", { device_id: id, offset });
      return () => {
        s.days = days;
      };
    });
  },

  async _rawRows(id, date, offset = 0, direction = null) {
    const s = this._recordedState(id);
    if (date !== s.date)
      Object.assign(s, { page: null, back: [], exportStatus: null });
    s.date = date;
    const prior = s.page;
    const parameters = { device_id: s.device, date, offset, limit: 100 };
    if (direction)
      Object.assign(parameters, { end: prior.end, revision: prior.revision });
    await this._rawRequest(id, async (call) => {
      const page = await call("recording_rows", parameters);
      return () => {
        if (direction === "next") s.back.push(prior.offset);
        else if (direction === "prev") s.back.pop();
        else s.back = [];
        s.page = page;
      };
    });
  },

  async _rawDownload(id) {
    const s = this._recordedState(id);
    if (s.export || !s.page) return;
    const task = { cancelled: false };
    const { device, date, page } = s;
    s.export = task;
    s.exportStatus = "Preparing CSV…";
    this._renderRecordedData(id);
    try {
      const chunks = await this._rawExportPages(id, task, {
        device_id: device,
        date,
        end: page.end,
        revision: page.revision,
      });
      if (task.cancelled)
        s.exportStatus = "Export cancelled. No partial file was downloaded.";
      else {
        this._download(
          new Blob(chunks, { type: "text/csv;charset=utf-8" }),
          `ld2410-${device}-${date}.csv`,
          "text/csv;charset=utf-8",
        );
        s.exportStatus = `CSV ready: ${date} (UTC), all 18 gate values. Missing values are empty cells.`;
      }
    } catch (error) {
      s.exportStatus = `Export failed: ${error?.message || error}. No partial file was downloaded.`;
    } finally {
      s.export = null;
      this._renderRecordedData(id);
    }
  },

  async _rawExportPages(id, task, parameters) {
    const chunks = [];
    let offset = 0;
    while (offset !== null && !task.cancelled) {
      const page = await this._queueChartCall(
        () => {
          if (task.cancelled || !this.isConnected) {
            task.cancelled = true;
            return null;
          }
          return this._call(
            "recording_rows",
            { ...parameters, offset, limit: 10000 },
            180000,
          );
        },
        { background: true },
      );
      // Leaving the panel while a page is in flight cancels the whole file.
      if (!this.isConnected) task.cancelled = true;
      if (task.cancelled) break;
      if (page.next_offset !== null && page.next_offset <= offset)
        throw new Error("Recording page did not advance");
      chunks.push(rawCsvPage(page, offset === 0));
      offset = page.next_offset;
      this._recordedState(id).exportStatus =
        `Exporting ${page.offset + page.rows.length} of ${page.total} rows…`;
      this._renderRecordedData(id);
    }
    return chunks;
  },
};
