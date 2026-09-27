export const panelJobs = {
  _learningJobHtml(d) {
    const job = d.learning_job;
    if (!job) return "";
    const running = job.status === "running";
    const stages = {
      preparing: "Checking radar settings",
      fitting: "Fitting and validating thresholds",
      saving: "Saving learned results",
    };
    const title = running
      ? stages[job.stage] || "Learning thresholds"
      : {
          completed: "Learning completed · result saved",
          error: "Learning could not finish",
          interrupted: "Learning was interrupted",
        }[job.status] || "Learning status";
    const elapsed = Math.max(
      0,
      Math.floor((job.finished_at || Date.now() / 1000) - job.started_at),
    );
    const duration =
      elapsed < 60
        ? `${elapsed}s`
        : `${Math.floor(elapsed / 60)}m ${elapsed % 60}s`;
    const source = job.sources?.includes("user")
      ? "Manual learn"
      : "Overnight learn";
    return `<div class="notice learning-job ${running ? "running" : ""}">
      <b>${this._esc(title)}</b><div class="muted">${source} · ${this._esc(duration)} ${running ? "elapsed" : "total"}</div>
      ${running ? '<progress aria-label="Learning in progress"></progress><div>You can leave this page. Learning continues in Home Assistant.</div>' : ""}
      ${job.error ? `<div class="job-error">${this._esc(job.error)}</div>` : ""}
      ${job.status === "completed" ? '<div>Review the accuracy report before applying. No thresholds were applied automatically.</div><button type="button" data-action="review-learning-job">Review result</button>' : ""}
    </div>`;
  },

  _refreshLearningProgress() {
    for (const card of this.shadowRoot.querySelectorAll("[data-device-id]")) {
      const id = card.dataset.deviceId;
      const device = this._data?.devices?.[id];
      if (!device) continue;
      const target = card.querySelector(".learning-job-status");
      if (target) target.innerHTML = this._learningJobHtml(device);
      this._wireLearningJob(card, id, device);
    }
  },

  _wireLearningJob(card, id, device) {
    const learn = card.querySelector('[data-action="learn"]');
    if (learn)
      learn.disabled =
        device.learning_job?.status === "running" || this._busyCards.has(id);
    const review = card.querySelector('[data-action="review-learning-job"]');
    if (review)
      review.onclick = () => {
        const results = device.learning_job.results || {};
        const slot = results.user ? "user" : "automatic";
        this._learningSelection.set(id, slot);
        this._setSectionCollapsed(id, "details", false);
        this._draw();
        this._openRecommendations(
          this.shadowRoot.querySelector(`[data-device-id="${CSS.escape(id)}"]`),
          id,
        );
      };
  },
};
