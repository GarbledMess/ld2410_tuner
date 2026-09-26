import { CHART_W, CHART_MARGIN, CHART_PLOT_W } from "./constants.js";

export const panelSelection = {
  _toLocalInputValue(ts) {
    const dt = new Date(ts * 1000);
    const pad = (n) => String(n).padStart(2, "0");
    return `${dt.getFullYear()}-${pad(dt.getMonth() + 1)}-${pad(dt.getDate())}T${pad(dt.getHours())}:${pad(dt.getMinutes())}`;
  },

  _selectionToolbarHtml(cs) {
    if (cs.historyLinked) {
      return '<div class="selection-toolbar muted">Drag to select or resize a period. Choose its status and save in the editor below.</div>';
    }
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

  _beginDrag(event, onMove, onUp, onCancel) {
    this._endDrag?.();
    this._dragging = true;
    const target = event.currentTarget;
    const pointerId = event.pointerId;
    const move = (next) => {
      if (next.pointerId === pointerId) onMove(next);
    };
    const end = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", cancel);
      window.removeEventListener("blur", cancel);
      target.removeEventListener("lostpointercapture", cancel);
      if (target.hasPointerCapture(pointerId))
        target.releasePointerCapture(pointerId);
      this._dragging = false;
      this._endDrag = null;
    };
    const up = (next) => {
      if (next.pointerId !== pointerId) return;
      end();
      onUp(next);
    };
    const cancel = (next) => {
      if (next?.pointerId != null && next.pointerId !== pointerId) return;
      end();
      onCancel();
    };
    this._endDrag = () => cancel();
    window.addEventListener("pointermove", move, { passive: false });
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", cancel);
    window.addEventListener("blur", cancel);
    target.addEventListener("lostpointercapture", cancel);
    if (event.isTrusted) target.setPointerCapture(pointerId);
  },

  _selectionPointerAllowed(event) {
    const id =
      event.currentTarget.closest("[data-device-id]")?.dataset.deviceId;
    return (
      event.isPrimary !== false &&
      event.button === 0 &&
      !this._dragging &&
      !this._busyCards.has(id)
    );
  },

  _wireChartSelection(id, container, d, cs) {
    const svg = container.querySelector("svg");
    if (!svg) return;
    // Keep handle targets finger-sized even when the SVG is scaled on a phone.
    const width = svg.getBoundingClientRect().width || CHART_W;
    const handleWidth = Math.max(22, (44 * CHART_W) / width);
    container.querySelectorAll(".handle-hit").forEach((hit) => {
      const center =
        Number(hit.getAttribute("x")) + Number(hit.getAttribute("width")) / 2;
      hit.setAttribute("width", handleWidth);
      hit.setAttribute("x", center - handleWidth / 2);
    });
    const restore = () => {
      cs.selection = previous ? { ...previous } : null;
      this._renderChartCanvas(id, true);
    };
    let previous;
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
        if (hit)
          hit.setAttribute(
            "x",
            (x - Number(hit.getAttribute("width")) / 2).toFixed(1),
          );
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
          if (!this._selectionPointerAllowed(ev)) return;
          previous = cs.selection ? { ...cs.selection } : null;
          ev.preventDefault();
          ev.stopPropagation();
          const onMove = (e2) => {
            e2.preventDefault();
            let t = xToTime(pxFromClientX(e2.clientX));
            if (which === "start") t = Math.min(t, cs.selection.end - 1);
            else t = Math.max(t, cs.selection.start + 1);
            cs.selection[which] = t;
            updateHandleVisual(which);
            syncPanelFields();
          };
          const onUp = () => {
            if (cs.historyLinked) this._useGraphSelection(id, cs.selection);
            this._renderChartCanvas(id);
          };
          this._beginDrag(ev, onMove, onUp, restore);
        };
      });

    // Dragging on empty plot space always starts a brand-new selection,
    // replacing any existing one (handles have their own listeners above
    // and stopPropagation, so a drag that starts on a handle never reaches
    // this).
    const hit = container.querySelector('[data-role="selection-hit"]');
    if (hit) {
      hit.onpointerdown = (ev) => {
        if (!this._selectionPointerAllowed(ev)) return;
        previous = cs.selection ? { ...cs.selection } : null;
        ev.preventDefault();
        const startX = pxFromClientX(ev.clientX);
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
          showTemp(startX, pxFromClientX(e2.clientX));
        };
        const onUp = (e2) => {
          const x = pxFromClientX(e2.clientX);
          const x0 = Math.min(startX, x),
            x1 = Math.max(startX, x);
          if (x1 - x0 < 4) {
            restore();
            return;
          }
          cs.selection = { start: xToTime(x0), end: xToTime(x1) };
          cs.panelOpen = false;
          if (cs.historyLinked) this._useGraphSelection(id, cs.selection);
          this._renderChartCanvas(id);
        };
        this._beginDrag(ev, onMove, onUp, restore);
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
        btn.onclick = () =>
          this._action(btn, async () => {
            const { start, end } = cs.selection;
            await this._call("label_history", {
              device_id: id,
              start: Math.floor(start),
              end: Math.floor(end),
              state: btn.dataset.label,
            });
            cs.selection = null;
            cs.panelOpen = false;
            cs.data = null;
            cs.cache?.clear();
            await this._load(true);
          });
      });
  },

  _updateGateChipHighlight(card, gate) {
    card.querySelectorAll(".gate-chip").forEach((btn) => {
      const selected = Number(btn.dataset.gate) === gate;
      btn.classList.toggle("active", selected);
      btn.setAttribute("aria-pressed", String(selected));
    });
  },
};
