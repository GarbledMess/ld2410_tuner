import { rawCsvPage } from "./recorded_csv.js";
import { recordedHtml } from "./recorded_view.js";

export const panelRecordedData = {
  _drawRecordedData() {
    if (this._recordedData) return;
    this._recordedData = { devices: [], kind: "move", request: 0, back: [] };
    const card = this.shadowRoot.querySelector("#recorded-data");
    card.ontoggle = () => {
      if (card.open && !this._recordedData.started) void this._rawBootstrap();
    };
    this._renderRecordedData();
  },

  _renderRecordedData() {
    const container = this.shadowRoot.querySelector("[data-raw-content]");
    this._updateHtml(
      container,
      recordedHtml(
        this._recordedData,
        (v) => this._esc(v),
        this._data?.devices,
      ),
    );
    container.onclick = (event) => {
      const button = event.target.closest("button");
      if (button && !button.disabled) this._rawAction(button.dataset);
    };
    container.onchange = (event) => {
      if (event.target.matches("[data-raw-device]"))
        void this._rawDays(event.target.value);
      if (event.target.matches("[data-raw-kind]")) {
        this._recordedData.kind = event.target.value;
        this._renderRecordedData();
      }
    };
  },

  _rawAction(data) {
    const s = this._recordedData;
    if ("rawInventory" in data) void this._rawBootstrap();
    if ("rawOpen" in data) void this._rawRows(data.rawOpen);
    if ("rawRefresh" in data) void this._rawRows(s.date);
    if ("rawNext" in data)
      void this._rawRows(s.date, s.page.next_offset, "next");
    if ("rawPrev" in data) void this._rawRows(s.date, s.back.at(-1), "prev");
    if ("rawDays" in data) void this._rawDays(s.device, Number(data.rawDays));
    if ("rawDownload" in data) void this._rawDownload();
    if ("rawCancel" in data) {
      s.export.cancelled = true;
      s.exportStatus = "Cancelling export after the current page…";
      this._renderRecordedData();
    }
  },

  async _rawRequest(work) {
    const s = this._recordedData;
    const request = ++s.request;
    s.loading = true;
    s.error = null;
    this._renderRecordedData();
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
        this._renderRecordedData();
      }
    }
  },

  async _rawBootstrap() {
    const s = this._recordedData;
    s.started = true;
    await this._rawRequest(async (call) => {
      const inventory = await call("recording_days");
      const device =
        inventory.devices.find((d) => d.id === s.device)?.id ||
        inventory.devices[0]?.id;
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

  async _rawDays(device, offset = 0) {
    const s = this._recordedData;
    Object.assign(s, {
      device,
      days: null,
      page: null,
      date: null,
      back: [],
      exportStatus: null,
    });
    await this._rawRequest(async (call) => {
      const days = await call("recording_days", { device_id: device, offset });
      return () => {
        s.days = days;
      };
    });
  },

  async _rawRows(date, offset = 0, direction = null) {
    const s = this._recordedData;
    if (date !== s.date)
      Object.assign(s, { page: null, back: [], exportStatus: null });
    s.date = date;
    const prior = s.page;
    const parameters = { device_id: s.device, date, offset, limit: 100 };
    if (direction)
      Object.assign(parameters, { end: prior.end, revision: prior.revision });
    await this._rawRequest(async (call) => {
      const page = await call("recording_rows", parameters);
      return () => {
        if (direction === "next") s.back.push(prior.offset);
        else if (direction === "prev") s.back.pop();
        else s.back = [];
        s.page = page;
      };
    });
  },

  async _rawDownload() {
    const s = this._recordedData;
    if (s.export || !s.page) return;
    const task = { cancelled: false };
    const { device, date, page } = s;
    s.export = task;
    s.exportStatus = "Preparing CSV…";
    this._renderRecordedData();
    try {
      const chunks = await this._rawExportPages(task, {
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
      this._renderRecordedData();
    }
  },

  async _rawExportPages(task, parameters) {
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
      this._recordedData.exportStatus = `Exporting ${page.offset + page.rows.length} of ${page.total} rows…`;
      this._renderRecordedData();
    }
    return chunks;
  },
};
