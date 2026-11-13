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
      if (el.textContent !== message) el.textContent = message;
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
    // Busy state is shared, so every copy of the radar is locked.
    const scopes = card
      ? [card, ...this._placements(id).filter((copy) => copy !== card)]
      : [this.shadowRoot];
    const fields = scopes
      .flatMap((scope) => [...scope.querySelectorAll("button, input, select")])
      .filter(
        (field) =>
          !field.matches(
            '.toggle, .sub-toggle, .subsection-head, [data-action="review-results"], [data-action="review-learning-job"]',
          ),
      );
    const disabled = fields.map((field) => [field, field.disabled]);
    for (const [field] of disabled) field.disabled = true;
    control.setAttribute("aria-busy", "true");
    const status =
      card?.querySelector(".action-status") ||
      control
        .closest("#storage-controls")
        ?.querySelector(".storage-action-status") ||
      control
        .closest("#timing-controls")
        ?.querySelector(".settings-action-status") ||
      control.closest("#bermuda")?.querySelector(".settings-action-status") ||
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
        recording: "Saving recording setting…",
        "bermuda-save": "Saving Bermuda settings…",
        "sources-save": "Saving presence sources…",
        "storage-save": "Saving global policy and cleaning history…",
        "storage-trim": "Trimming recorded data…",
        "timing-save": "Saving global timing settings…",
        "nightly-save": "Saving global learning settings…",
        "device-auto-apply": "Saving automatic Apply override…",
        apply: "Applying thresholds and checking reported values…",
        clear: "Clearing device data…",
        json: "Preparing JSON export…",
        csv: "Preparing CSV export…",
        "auto-feedback": "Saving feedback…",
        "refresh-references": "Queueing a reference refresh…",
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

  // Room labels are most of a snapshot and rarely change: the server omits a room's labels
  // when the token sent matches, and they are restored from the last copy it did send.
  async _snapshotWithRoomLabels() {
    const kept = this._roomLabelCache || {};
    const tokens = Object.fromEntries(
      Object.entries(kept).map(([gid, item]) => [gid, item.token]),
    );
    const known = Object.keys(tokens).length
      ? { room_label_tokens: tokens }
      : {};
    let data = await this._call("snapshot", known);
    let restored = this._restoreRoomLabels(data, kept);
    if (!restored) {
      // Never draw a room without its labels: ask again for every room's labels.
      data = await this._call("snapshot");
      restored = this._restoreRoomLabels(data, {});
    }
    this._roomLabelCache = restored;
    return data;
  },

  _restoreRoomLabels(data, kept) {
    const next = {};
    for (const [gid, group] of Object.entries(data?.rooms?.groups || {})) {
      if (group.labels_unchanged) {
        if (kept[gid]?.token !== group.labels_token) return null;
        group.labels = kept[gid].labels;
        delete group.labels_unchanged;
      }
      if (group.labels_token)
        next[gid] = { token: group.labels_token, labels: group.labels };
    }
    return next;
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
    // Routine polls stay quiet; initial loading and explicit actions have indicators.
    const timer = this._loaded ? null : setTimeout(show, 0);
    try {
      this._data = await this._snapshotWithRoomLabels();
      this._drawWorkers();
      this._drawHistorySaveWarning();
      this._ensureComparisons();
      this._refreshRecoveryProgress();
      this._refreshLearningProgress();
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
        const layout = this.shadowRoot?.querySelector("#layout");
        if (layout)
          layout.innerHTML = `<div class="card">Unable to load LD2410 Tuner: ${this._esc(err?.message || err)}</div>`;
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
