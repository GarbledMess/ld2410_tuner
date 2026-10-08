import { SLOT_LABELS } from "./saved_results.js";

export const panelRoomLearning = {
  _roomLearningHtml(id, group, rooms) {
    const state = group.learning || {};
    const busy = state.job?.status === "running";
    const slots = state.slots || {};
    const slot =
      this._jointSlots?.get(id) || (slots.user ? "user" : "automatic");
    const names = group.device_ids
      .map((key) => rooms.members[key]?.name || key)
      .join(", ");
    const status = busy
      ? "Joint learning in progress; you can leave and return."
      : state.job?.error || "";
    return `<section class="joint-learning"><h3>Learn this zone together</h3>
      <p>Members: ${this._esc(names)}. One radar can cover another's misses. Strong overlapping coverage from both is retained when their signals separate from noise. Any member's false trigger counts against the zone.</p>
      ${this._jointLearnButtonHtml(group, rooms, busy)}
      <p role="status" class="${busy ? "is-busy" : ""}">${this._esc(status)}</p>
      ${this._jointResultHtml(group, rooms.members, slot)}
      ${this._jointApplicationHtml(state.application)}
      ${this._automaticApplyHtml(state.automatic_apply)}
      <p class="muted">Each radar keeps its own timeout and delays. All valid readings in shared coverage are used, including noise spikes. Human evidence has priority; confidence-weighted estimates fill missing evidence and break ties. Automatic Apply requires every member to permit this learning source, and a lower joint time penalty on fresh shared recordings. Only thresholds are written.</p></section>`;
  },

  _jointLearnButtonHtml(group, rooms, busy) {
    const subdivided =
      group.kind === "area" &&
      Object.values(rooms.groups).some(
        (item) => item.kind === "zone" && item.area_id === group.area_id,
      );
    if (subdivided)
      return "<p>Learn the independent zones below; whole-area learning would mix their occupancy.</p>";
    return `<button data-room-action="learn" ${busy ? 'disabled class="is-busy"' : ""}>Learn together · last 7 days</button>`;
  },

  _jointResultHtml(group, members, slot) {
    const state = group.learning || {};
    const slots = state.slots || {};
    const result = slots[slot];
    if (!result) return "";
    const options = ["user", "automatic"]
      .map(
        (key) =>
          `<option value="${key}" ${key === slot ? "selected" : ""} ${slots[key] ? "" : "disabled"}>${SLOT_LABELS[key]}</option>`,
      )
      .join("");
    const busy =
      state.job?.status === "running" ||
      state.application?.status === "applying";
    return `<label>Joint result <select data-joint-slot>${options}</select></label><p>Stored zone score: ${this._scoreText(result.before?.room)} → ${this._scoreText(result.after?.room)}. Same recordings and timing; training replay, not an independent accuracy test.</p>
      ${this._roomOutcomeHtml(result.after.room)}${this._jointThresholdsHtml(result, members)}
      <button data-room-action="apply-joint" class="apply-${result.status === "unsafe" ? "bad" : "caution"}" ${busy ? "disabled" : ""}>Apply to all ${group.device_ids.length} radars</button>`;
  },

  _jointThresholdsHtml(result, members) {
    return Object.entries(result.thresholds || {})
      .map(([device, gates]) => {
        const changes = Object.entries(gates)
          .map(
            ([key, value]) =>
              `${key}: ${result.signature[device].thresholds[key]} → ${value}`,
          )
          .join(" · ");
        return `<p><b>${this._esc(members[device]?.name || device)}</b>: ${this._esc(changes)}</p>`;
      })
      .join("");
  },

  _jointApplicationHtml(application) {
    if (!application) return "";
    const errors = Object.values(application.writes?.skipped || {});
    const warning = errors.length
      ? ` ${errors.join("; ")} Already written values remain; check all radars and learn again.`
      : "";
    return `<p role="status">Joint Apply: ${this._esc(application.status)}. ${this._esc(application.reason || "")}${this._esc(warning)}</p>`;
  },

  _wireRoomLearning(section, id, group) {
    const learn = section.querySelector('[data-room-action="learn"]');
    if (learn)
      learn.onclick = () =>
        this._action(learn, async () => {
          await this._call("learn_room", { group_id: id });
          await this._load(true);
        });
    const select = section.querySelector("[data-joint-slot]");
    if (select)
      select.onchange = () => {
        this._jointSlots ||= new Map();
        this._jointSlots.set(id, select.value);
        select.blur();
        this._drawRooms();
      };
    const apply = section.querySelector('[data-room-action="apply-joint"]');
    if (apply)
      apply.onclick = () =>
        this._action(apply, async () => {
          const source = select.value;
          const result = group.learning.slots[source];
          if (
            !confirm(
              `Apply the complete joint recommendation to all ${group.device_ids.length} radars in ${group.name}? Review every member's thresholds above. Writes are sequential; a failure may leave partial changes.`,
            )
          )
            return;
          const outcome = await this._call(
            "apply_room",
            { group_id: id, result_id: result.id, source },
            Math.max(240000, group.device_ids.length * 120000),
          );
          await this._load(true);
          if (Object.keys(outcome.skipped || {}).length)
            throw new Error(
              "Joint Apply stopped partway. Check the group report and live thresholds before learning again.",
            );
          if (outcome.note) alert(outcome.note);
        });
  },

  _openJointResult(joint) {
    const rooms = this.shadowRoot.querySelector("#rooms");
    rooms.open = true;
    this._roomOpen ||= new Set();
    this._roomOpen.add(joint.group_id);
    this._drawRooms();
    const group = [...rooms.querySelectorAll("[data-room-id]")].find(
      (item) => item.dataset.roomId === joint.group_id,
    );
    if (!group)
      throw new Error(
        "This joint group was removed. Configure its members and learn again.",
      );
    group.scrollIntoView({ behavior: "smooth", block: "nearest" });
  },
};
