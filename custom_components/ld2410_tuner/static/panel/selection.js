import { CHART_W, CHART_MARGIN, CHART_PLOT_W } from "./constants.js";

export const panelSelection = {
  _toLocalInputValue(ts) {
    const dt = new Date(ts * 1000);
    const pad = (n) => String(n).padStart(2, "0");
    return `${dt.getFullYear()}-${pad(dt.getMonth() + 1)}-${pad(dt.getDate())}T${pad(dt.getHours())}:${pad(dt.getMinutes())}`;
  },

  _selectionToolbarHtml(cs) {
    if (!cs.selection) {
      return `<div class="selection-toolbar muted">Drag across the chart to select a time range to label. Once selected, drag either end to fine-tune.</div>`;
    }
    const fmt = (t) =>
      new Date(t * 1000).toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
    if (!cs.panelOpen) {
      return `<div class="selection-toolbar">
        <span class="selection-range">${this._esc(fmt(cs.selection.start))} → ${this._esc(fmt(cs.selection.end))}</span>
        <button type="button" class="fb-btn" data-action="selection-finetune">Fine-tune &amp; set status</button>
        <button type="button" class="fb-btn" data-action="selection-cancel">Cancel</button>
      </div>`;
    }
    return `<div class="selection-toolbar selection-panel">
      <div class="history-fields" style="grid-template-columns:1fr 1fr;">
        <label>From<input type="datetime-local" data-role="selection-from" value="${this._toLocalInputValue(cs.selection.start)}"></label>
        <label>To<input type="datetime-local" data-role="selection-to" value="${this._toLocalInputValue(cs.selection.end)}"></label>
      </div>
      <div class="selection-buttons">
        <button type="button" class="fb-btn" data-action="selection-label" data-label="present">Present</button>
        <button type="button" class="fb-btn" data-action="selection-label" data-label="not_present">Not present</button>
        <button type="button" class="fb-btn" data-action="selection-label" data-label="unknown">Unknown</button>
        <button type="button" class="fb-btn" data-action="selection-exit">Exit</button>
      </div>
    </div>`;
  },

  _beginDrag(onMove, onUp) {
    this._endDrag?.();
    this._dragging = true;
    const end = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", cancel);
      window.removeEventListener("blur", cancel);
      this._dragging = false;
      this._endDrag = null;
    };
    const up = (event) => {
      end();
      onUp(event);
    };
    const cancel = () => {
      end();
      this._draw();
    };
    this._endDrag = end;
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", cancel);
    window.addEventListener("blur", cancel);
  },

  _wireChartSelection(id, container, d, cs) {
    const svg = container.querySelector("svg");
    if (!svg) return;
    const data = cs.data;
    const span = Math.max(1, data.end - data.start);
    const xScale = (t) =>
      CHART_MARGIN.left + ((t - data.start) / span) * CHART_PLOT_W;
    const xToTime = (x) =>
      data.start + ((x - CHART_MARGIN.left) / CHART_PLOT_W) * span;
    const pxFromClientX = (clientX) => {
      const rect = svg.getBoundingClientRect();
      const scale = rect.width ? CHART_W / rect.width : 1;
      return Math.max(
        CHART_MARGIN.left,
        Math.min(CHART_W - CHART_MARGIN.right, (clientX - rect.left) * scale),
      );
    };
    const clientXOf = (ev) => {
      if (ev.touches) return ev.touches[0].clientX;
      return ev.changedTouches ? ev.changedTouches[0].clientX : ev.clientX;
    };

    const updateHandleVisual = (which) => {
      const x = xScale(cs.selection[which]);
      const g = container.querySelector(`[data-role="handle-${which}"]`);
      if (g) {
        const line = g.querySelector("line"),
          circle = g.querySelector("circle"),
          hit = g.querySelector(".handle-hit");
        if (line) {
          line.setAttribute("x1", x.toFixed(1));
          line.setAttribute("x2", x.toFixed(1));
        }
        if (circle) circle.setAttribute("cx", x.toFixed(1));
        if (hit) hit.setAttribute("x", (x - 11).toFixed(1));
      }
      const rect = container.querySelector('[data-role="selection-rect"]');
      if (rect) {
        const x0 = xScale(cs.selection.start),
          x1 = xScale(cs.selection.end);
        rect.setAttribute("x", Math.min(x0, x1).toFixed(1));
        rect.setAttribute("width", Math.abs(x1 - x0).toFixed(1));
      }
    };
    const syncPanelFields = () => {
      if (!cs.panelOpen) return;
      const fromInput = container.querySelector('[data-role="selection-from"]');
      const toInput = container.querySelector('[data-role="selection-to"]');
      if (fromInput)
        fromInput.value = this._toLocalInputValue(cs.selection.start);
      if (toInput) toInput.value = this._toLocalInputValue(cs.selection.end);
    };

    container
      .querySelectorAll('[data-role="handle-start"], [data-role="handle-end"]')
      .forEach((g) => {
        const which = g.dataset.role === "handle-start" ? "start" : "end";
        g.onpointerdown = (ev) => {
          ev.preventDefault();
          ev.stopPropagation();
          this._dragging = true;
          const onMove = (e2) => {
            e2.preventDefault();
            let t = xToTime(pxFromClientX(clientXOf(e2)));
            if (which === "start") t = Math.min(t, cs.selection.end - 1);
            else t = Math.max(t, cs.selection.start + 1);
            cs.selection[which] = t;
            updateHandleVisual(which);
            syncPanelFields();
          };
          const onUp = () => {
            window.removeEventListener("pointermove", onMove);
            this._dragging = false;
            this._renderChartCanvas(id);
          };
          this._beginDrag(onMove, onUp);
        };
      });

    // Dragging on empty plot space always starts a brand-new selection,
    // replacing any existing one (handles have their own listeners above
    // and stopPropagation, so a drag that starts on a handle never reaches
    // this).
    const hit = container.querySelector('[data-role="selection-hit"]');
    if (hit) {
      hit.onpointerdown = (ev) => {
        ev.preventDefault();
        this._dragging = true;
        const startX = pxFromClientX(clientXOf(ev));
        const selRect = container.querySelector('[data-role="selection-rect"]');
        const showTemp = (x0, x1) => {
          if (!selRect) return;
          selRect.style.display = "block";
          selRect.setAttribute("x", Math.min(x0, x1).toFixed(1));
          selRect.setAttribute("width", Math.abs(x1 - x0).toFixed(1));
        };
        showTemp(startX, startX);
        const onMove = (e2) => {
          e2.preventDefault();
          showTemp(startX, pxFromClientX(clientXOf(e2)));
        };
        const onUp = (e2) => {
          window.removeEventListener("pointermove", onMove);
          this._dragging = false;
          const x = pxFromClientX(clientXOf(e2));
          const x0 = Math.min(startX, x),
            x1 = Math.max(startX, x);
          if (x1 - x0 < 4) {
            if (selRect) selRect.style.display = "none";
            return;
          }
          cs.selection = { start: xToTime(x0), end: xToTime(x1) };
          cs.panelOpen = false;
          this._renderChartCanvas(id);
        };
        this._beginDrag(onMove, onUp);
      };
    }

    const finetuneBtn = container.querySelector(
      '[data-action="selection-finetune"]',
    );
    if (finetuneBtn)
      finetuneBtn.onclick = () => {
        cs.panelOpen = true;
        this._renderChartCanvas(id);
      };
    const cancelBtn = container.querySelector(
      '[data-action="selection-cancel"]',
    );
    if (cancelBtn)
      cancelBtn.onclick = () => {
        cs.selection = null;
        cs.panelOpen = false;
        this._renderChartCanvas(id);
      };
    const exitBtn = container.querySelector('[data-action="selection-exit"]');
    if (exitBtn)
      exitBtn.onclick = () => {
        cs.selection = null;
        cs.panelOpen = false;
        this._renderChartCanvas(id);
      };

    const fromInput = container.querySelector('[data-role="selection-from"]');
    if (fromInput)
      fromInput.oninput = () => {
        const t = Math.floor(new Date(fromInput.value).getTime() / 1000);
        if (!Number.isFinite(t)) return;
        cs.selection.start = Math.min(t, cs.selection.end - 1);
        updateHandleVisual("start");
        if (cs.selection.start !== t)
          fromInput.value = this._toLocalInputValue(cs.selection.start);
      };
    const toInput = container.querySelector('[data-role="selection-to"]');
    if (toInput)
      toInput.oninput = () => {
        const t = Math.floor(new Date(toInput.value).getTime() / 1000);
        if (!Number.isFinite(t)) return;
        cs.selection.end = Math.max(t, cs.selection.start + 1);
        updateHandleVisual("end");
        if (cs.selection.end !== t)
          toInput.value = this._toLocalInputValue(cs.selection.end);
      };

    container
      .querySelectorAll('[data-action="selection-label"]')
      .forEach((btn) => {
        btn.onclick = async () => {
          const label = btn.dataset.label;
          const { start, end } = cs.selection;
          try {
            await this._call("label_history", {
              device_id: id,
              start: Math.floor(start),
              end: Math.floor(end),
              state: label,
            });
            cs.selection = null;
            cs.panelOpen = false;
            cs.data = null;
            cs.cache?.clear();
            cs.cache?.clear();
          } catch (err) {
            alert(`Unable to label history: ${err?.message || err}`);
            return;
          }
          await this._load();
        };
      });
  },

  _updateGateChipHighlight(card, gate) {
    card.querySelectorAll(".gate-chip").forEach((btn) => {
      btn.classList.toggle("active", Number(btn.dataset.gate) === gate);
    });
  },
};
