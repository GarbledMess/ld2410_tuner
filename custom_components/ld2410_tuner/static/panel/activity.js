export const panelActivity = {
  async _call(type, data = {}, timeout = 30000) {
    let timer;
    try {
      return await Promise.race([
        this._hass.callWS({ type: `ld2410_tuner/${type}`, ...data }),
        new Promise((_, reject) => {
          timer = setTimeout(
            () =>
              reject(
                new Error(
                  "Request timed out. Refresh before retrying a change; it may have completed.",
                ),
              ),
            timeout,
          );
        }),
      ]);
    } finally {
      clearTimeout(timer);
    }
  },

  _showError(message) {
    const el = this.shadowRoot?.querySelector("#error");
    if (el) {
      el.textContent = message;
      el.hidden = !message;
    }
  },

  _beginAction(control) {
    const card = control.closest("[data-device-id]");
    const id = card?.dataset.deviceId;
    if (this._busyCards.has(id)) return null;
    this._busyCards.add(id);
    this._activeActions++;
    // Keep navigation available; lock edits until the current write settles.
    const fields = [
      ...(card || this.shadowRoot).querySelectorAll("button, input, select"),
    ].filter(
      (field) =>
        !field.matches(
          '.toggle, .sub-toggle, .subsection-head, [data-action="review-results"]',
        ),
    );
    const disabled = fields.map((field) => [field, field.disabled]);
    for (const [field] of disabled) field.disabled = true;
    control.setAttribute("aria-busy", "true");
    const status =
      card?.querySelector(".action-status") ||
      this.shadowRoot.querySelector("#snapshot-status");
    if (status) {
      status.textContent = this._actionLabel(control.dataset.action);
      status.classList.add("is-busy");
    }
    return { id, control, disabled, status };
  },

  _actionLabel(action) {
    return (
      {
        learn: "Learning thresholds…",
        "nightly-save": "Saving overnight schedule…",
        apply: "Applying thresholds and checking reported values…",
        clear: "Clearing device data…",
        json: "Preparing JSON export…",
        csv: "Preparing CSV export…",
        "auto-feedback": "Saving feedback…",
        "history-apply": "Saving presence labels…",
        "history-remove": "Removing presence label…",
        "selection-label": "Saving presence labels…",
      }[action] || "Saving training settings…"
    );
  },

  _endAction(operation) {
    for (const [field, disabled] of operation.disabled)
      field.disabled = disabled;
    operation.control.removeAttribute("aria-busy");
    if (operation.status) {
      operation.status.textContent = "";
      operation.status.classList.remove("is-busy");
    }
    this._busyCards.delete(operation.id);
    this._activeActions--;
    if (this._redrawPending && !this._isEditing()) {
      this._redrawPending = false;
      this._draw();
    }
  },

  async _action(control, action) {
    const operation = this._beginAction(control);
    if (!operation) return;
    this._actionError = "";
    this._showError("");
    try {
      await action();
    } catch (err) {
      this._actionError = err?.message || String(err);
      this._showError(this._actionError);
      this.shadowRoot?.querySelector("#error")?.scrollIntoView({
        behavior: "smooth",
        block: "nearest",
      });
    } finally {
      this._endAction(operation);
    }
  },

  _load(fresh = false) {
    if (!this._hass) return Promise.resolve();
    if (this._loadPromise)
      return fresh
        ? this._loadPromise.then(() => this._load())
        : this._loadPromise;
    this._loadPromise = this._loadSnapshot().finally(() => {
      this._loadPromise = null;
    });
    return this._loadPromise;
  },

  async _loadSnapshot() {
    this._loading = true;
    const status = this.shadowRoot?.querySelector("#snapshot-status");
    const show = () => {
      if (!status) return;
      status.textContent = this._loaded
        ? "Refreshing device readings…"
        : "Loading devices…";
      status.classList.add("is-busy");
    };
    // Avoid a flashing spinner for quick background polls.
    const timer = setTimeout(show, this._loaded ? 400 : 0);
    try {
      this._data = await this._call("snapshot");
      this._showError(this._actionError);
      this._loaded = true;
      if (this._isEditing()) {
        this._redrawPending = true;
      } else {
        this._draw();
      }
    } catch (err) {
      this._showError(`Unable to refresh: ${err?.message || err}`);
      if (!this._loaded) {
        const grid = this.shadowRoot?.querySelector("#grid");
        if (grid)
          grid.innerHTML = `<div class="card">Unable to load LD2410 Tuner: ${this._esc(err?.message || err)}</div>`;
      }
    } finally {
      clearTimeout(timer);
      if (status) {
        status.textContent = "";
        status.classList.remove("is-busy");
      }
      this._loading = false;
    }
  },
};
