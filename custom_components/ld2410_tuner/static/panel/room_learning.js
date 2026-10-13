import { SLOT_LABELS } from "./saved_results.js";

// Learning sets (spec 5): multi-radar groups linked by a shared radar learn
// together. A one-group set learns in its group's header; a linked set
// learns in a band ("Linked: Desk + Sofa · share Door radar") and its zone
// headers show their own before → after.
const ZONES_LEARN_ON_THEIR_OWN =
  "Learn each zone below in its own header; whole-area learning would mix their occupancy.";

export const panelRoomLearning = {
  _learningSets() {
    return this._data?.learning_sets || {};
  },

  _findSet(test) {
    const sets = this._learningSets();
    const id = Object.keys(sets).find((key) => test(sets[key]));
    return id ? { id, ...sets[id] } : null;
  },

  _setOfGroup(groupId) {
    return this._findSet((set) => set.group_ids.includes(groupId));
  },

  _setOfDevice(deviceId) {
    return this._findSet((set) => set.device_ids.includes(deviceId));
  },

  _linkedSets() {
    return Object.entries(this._learningSets())
      .filter(([, set]) => set.group_ids.length > 1)
      .map(([id, set]) => ({ id, ...set }));
  },

  _setName(set) {
    return set.group_ids.map((key) => this._groupName(key)).join(" + ");
  },

  _sharedRadarNames(set) {
    const rooms = this._roomsData();
    const counts = new Map();
    for (const key of set.group_ids)
      for (const radar of rooms.groups[key]?.device_ids || [])
        counts.set(radar, (counts.get(radar) || 0) + 1);
    return [...counts]
      .filter(([, count]) => count > 1)
      .map(([radar]) => rooms.members[radar]?.name || radar)
      .join(", ");
  },

  _setSlot(id, learning) {
    const slots = learning?.slots || {};
    return this._jointSlots?.get(id) || (slots.user ? "user" : "automatic");
  },

  _setResult(id, learning) {
    return learning?.slots?.[this._setSlot(id, learning)] || null;
  },

  _learningBusy(state) {
    return state?.job?.status === "running";
  },

  // A set's job status line: progress while it runs, otherwise its last error.
  _learningStatusHtml(state, kind) {
    const busy = this._learningBusy(state);
    const text = busy
      ? `${kind} learning in progress; you can leave and return.`
      : state.job?.error || "";
    return `<p role="status" class="${busy ? "is-busy" : ""}">${this._esc(text)}</p>`;
  },

  _groupName(key) {
    return this._roomsData().groups[key]?.name || key;
  },

  // ---- Group headers -----------------------------------------------------

  _groupLearningHtml(id, group, rooms) {
    const set = this._setOfGroup(id);
    if (!set) return this._ungroupedLearningHtml(group, rooms);
    if (set.group_ids.length > 1) return this._linkedZoneHtml(id, set);
    return this._roomLearningHtml(
      id,
      { ...group, learning: set.learning },
      rooms,
    );
  },

  _ungroupedLearningHtml(group, rooms) {
    if (this._subdivided(group, rooms))
      return `<section class="joint-learning"><p>${this._zonedAreaText(group, rooms)}</p></section>`;
    return '<section class="joint-learning"><p class="muted">One radar: it learns on its own card.</p></section>';
  },

  // Linked zones of this area learn in their band, not in a zone header.
  _zonedAreaText(group, rooms) {
    const linked = this._linkedSets().some((set) =>
      set.group_ids.some(
        (key) =>
          rooms.groups[key]?.kind === "zone" &&
          rooms.groups[key].area_id === group.area_id,
      ),
    );
    return linked
      ? "Zones that share a radar learn together in their Linked band. Learn other zones in their own headers; whole-area learning would mix their occupancy."
      : ZONES_LEARN_ON_THEIR_OWN;
  },

  _subdivided(group, rooms) {
    return (
      group.kind === "area" &&
      Object.values(rooms.groups).some(
        (item) => item.kind === "zone" && item.area_id === group.area_id,
      )
    );
  },

  _linkedZoneHtml(id, set) {
    const state = set.learning || {};
    const result = this._setResult(set.id, state);
    const change = result
      ? `<p>This zone: ${this._esc(this._zoneChangeText(result.groups?.[id]))}</p>`
      : "";
    return `<section class="joint-learning linked-zone"><p>Learned with <button type="button" class="link-button" data-room-action="show-set">Linked: ${this._esc(this._setName(set))} ↓</button></p>
      ${this._learningStatusHtml(state, "Linked")}${change}</section>`;
  },

  _zoneChangeText(zone) {
    if (!zone) return "not in this result";
    if (zone.skipped) return `skipped: ${zone.skipped}`;
    return `${this._scoreText(zone.before?.room)} → ${this._scoreText(zone.after?.room)}`;
  },

  _roomLearningHtml(id, group, rooms) {
    const state = group.learning || {};
    const busy = this._learningBusy(state);
    const names = group.device_ids
      .map((key) => rooms.members[key]?.name || key)
      .join(", ");
    return `<section class="joint-learning"><h3>Learn this zone together</h3>
      <p>Members: ${this._esc(names)}. One radar can cover another's misses. Strong overlapping coverage from both is retained when their signals separate from noise. Any member's false trigger counts against the zone.</p>
      ${this._jointLearnButtonHtml(group, rooms, busy)}
      ${this._learningStatusHtml(state, "Joint")}
      ${this._jointResultHtml(id, group, rooms.members)}
      ${this._jointApplicationHtml(state.application)}
      ${this._automaticApplyHtml(state.automatic_apply)}
      <p class="muted">Each radar keeps its own timeout and delays. All valid readings in shared coverage are used, including noise spikes. Human evidence has priority; confidence-weighted estimates fill missing evidence and break ties. Automatic Apply requires every member to permit this learning source, and a lower joint time penalty on fresh shared recordings. Only thresholds are written.</p></section>`;
  },

  _jointLearnButtonHtml(group, rooms, busy) {
    if (this._subdivided(group, rooms))
      return `<p>${ZONES_LEARN_ON_THEIR_OWN}</p>`;
    return `<button data-room-action="learn" ${busy ? 'disabled class="is-busy"' : ""}>Learn together · last 7 days</button>`;
  },

  _slotSelectHtml(slot, slots) {
    const options = ["user", "automatic"]
      .map(
        (key) =>
          `<option value="${key}" ${key === slot ? "selected" : ""} ${slots[key] ? "" : "disabled"}>${SLOT_LABELS[key]}</option>`,
      )
      .join("");
    return `<label>Joint result <select data-joint-slot>${options}</select></label>`;
  },

  _applyButtonHtml(state, result, count) {
    const busy =
      this._learningBusy(state) || state.application?.status === "applying";
    return `<button data-room-action="apply-joint" class="apply-${result.status === "unsafe" ? "bad" : "caution"}" ${busy ? "disabled" : ""}>Review & apply · all ${count} radars</button>`;
  },

  _jointResultHtml(id, group, members) {
    const state = group.learning || {};
    const slot = this._setSlot(id, state);
    const result = state.slots?.[slot];
    if (!result) return "";
    return `${this._slotSelectHtml(slot, state.slots)}<p>Stored zone score: ${this._scoreText(result.before?.room)} → ${this._scoreText(result.after?.room)}. Same recordings and timing; training replay, not an independent accuracy test.</p>
      ${this._roomOutcomeHtml(result.after.room)}${this._jointThresholdsHtml(result, members)}
      ${this._applyButtonHtml(state, result, group.device_ids.length)}`;
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

  // ---- Linked-set band ---------------------------------------------------

  _linkedBandHtml(set) {
    const state = set.learning || {};
    const busy = this._learningBusy(state);
    const shared = this._sharedRadarNames(set);
    return `<h3>Linked: ${this._esc(this._setName(set))}${shared ? ` · share ${this._esc(shared)}` : ""}</h3>
      <p>These zones share a radar, so they learn together. Each zone is scored on its own room labels; the set's error cost is their sum. There is no single room score for linked zones.</p>
      <p>Zone scores: ${this._esc(this._setZoneScores(set))}</p>
      <button data-room-action="learn" ${busy ? 'disabled class="is-busy"' : ""}>Learn linked zones</button>
      ${this._learningStatusHtml(state, "Linked")}
      ${this._linkedResultHtml(set)}
      ${this._jointApplicationHtml(state.application)}
      ${this._automaticApplyHtml(state.automatic_apply)}
      <p class="muted">Each radar keeps its own timeout and delays. Automatic Apply requires every member to permit this learning source, every zone to have usable labels, a lower total cost, and no zone getting worse. Only thresholds are written.</p>`;
  },

  // Each zone's latest assessed room score; the set has no single score.
  _setZoneScores(set) {
    const groups = this._roomsData().groups;
    return set.group_ids
      .map(
        (key) =>
          `${this._groupName(key)} ${this._scoreText(groups[key]?.assessment?.room)}`,
      )
      .join(" · ");
  },

  _linkedResultHtml(set) {
    const state = set.learning || {};
    const slot = this._setSlot(set.id, state);
    const result = state.slots?.[slot];
    if (!result) return "";
    const zones = set.group_ids
      .map(
        (key) =>
          `<li>${this._esc(this._groupName(key))}: ${this._esc(this._zoneChangeText(result.groups?.[key]))}</li>`,
      )
      .join("");
    return `${this._slotSelectHtml(slot, state.slots)}<ul class="linked-zones">${zones}</ul>
      <p>Total error cost: ${this._costText(result.cost_before)} → ${this._costText(result.cost_after)} (lower is better). Same recordings and timing; training replay, not an independent accuracy test.</p>
      ${this._jointThresholdsHtml(result, this._roomsData().members)}
      ${this._applyButtonHtml(state, result, set.device_ids.length)}`;
  },

  _costText(value) {
    return value == null ? "—" : Number(value).toFixed(3);
  },

  // ---- Wiring ------------------------------------------------------------

  _wireRoomLearning(section, id, group) {
    const set = this._setOfGroup(id);
    if (!set) return;
    const show = section.querySelector('[data-room-action="show-set"]');
    if (show) show.onclick = () => this._revealSet(set.id);
    if (set.group_ids.length === 1)
      this._wireSetLearning(section, {
        id,
        learning: set.learning,
        name: group.name,
        count: set.device_ids.length,
      });
  },

  _wireSetLearning(section, target) {
    const learn = section.querySelector('[data-room-action="learn"]');
    if (learn)
      learn.onclick = () =>
        this._action(learn, async () => {
          await this._call("learn_set", { set_id: target.id });
          await this._load(true);
        });
    const select = section.querySelector("[data-joint-slot]");
    if (select)
      select.onchange = () => {
        this._jointSlots ||= new Map();
        this._jointSlots.set(target.id, select.value);
        select.blur();
        this._drawLayout();
      };
    const apply = section.querySelector('[data-room-action="apply-joint"]');
    if (apply)
      apply.onclick = () =>
        this._action(apply, () => this._applySet(target, select.value));
  },

  async _applySet(target, source) {
    const result = target.learning.slots[source];
    if (
      !confirm(
        `Apply the complete joint recommendation to all ${target.count} radars in ${target.name}? Review every member's thresholds above. Writes are sequential; a failure may leave partial changes.`,
      )
    )
      return;
    const outcome = await this._call(
      "apply_set",
      { set_id: target.id, result_id: result.id, source },
      Math.max(240000, target.count * 120000),
    );
    await this._load(true);
    if (Object.keys(outcome.skipped || {}).length)
      throw new Error(
        "Joint Apply stopped partway. Check the group report and live thresholds before learning again.",
      );
    if (outcome.note) alert(outcome.note);
  },

  _openJointResult(joint) {
    const group = this._revealGroup(joint.group_id);
    if (!group)
      throw new Error(
        "This joint group was removed. Configure its members and learn again.",
      );
    group.scrollIntoView({ behavior: "smooth", block: "nearest" });
  },

  // Show where a set learns: its band, or a one-group set's header.
  _revealSet(setId) {
    const set = this._learningSets()[setId];
    if (!set) return;
    if (set.group_ids.length === 1)
      return this._openJointResult({ group_id: set.group_ids[0] });
    this._drawLayout();
    const band = this.shadowRoot.querySelector(
      `#layout [data-set-id="${CSS.escape(setId)}"]`,
    );
    if (!band) return;
    this._openAncestors(band);
    band.scrollIntoView({ behavior: "smooth", block: "nearest" });
  },

  // ---- Radar cards -------------------------------------------------------

  // A grouped radar learns with its set: no Learn/Apply on the card.
  _learnedWithHtml(id, d, set) {
    const name = this._setName(set);
    const result = this._setResult(set.id, set.learning);
    return `<div class="learned-with"><p><b>Thresholds: learned with <button type="button" class="link-button" data-action="show-set" data-learning-set="${this._esc(set.id)}">${this._esc(name)} ↑</button></b></p>
      ${this._learnedWithRowsHtml(id, d, result)}
      ${this._manualEditWarningHtml(id, d, set.learning, name)}
      <p class="muted">Learn and Apply for this radar happen with its set. Manual gate edits in Home Assistant remain possible, but the set's result will become stale.</p></div>`;
  },

  _learnedWithRowsHtml(id, d, result) {
    if (!result) return '<p class="muted">No set result yet.</p>';
    const before = result.signature?.[id]?.thresholds || {};
    const proposed = result.thresholds?.[id] || {};
    const current = d.current_thresholds || {};
    const applied = d.last_applied || {};
    const rows = Object.keys(proposed)
      .filter((key) => proposed[key] !== before[key])
      .map(
        (key) =>
          `<li>${this._esc(key)}: current ${this._fmt(current[key])} · proposed ${this._fmt(proposed[key])}${key in applied ? ` · last applied ${this._fmt(applied[key])}` : ""}</li>`,
      );
    return rows.length
      ? `<ul class="learned-with-gates">${rows.join("")}</ul>`
      : '<p class="muted">No threshold changes proposed for this radar.</p>';
  },

  // Values the tuner itself produced for a gate: every saved slot's
  // learned-from value and proposal, and the radar's last applied value.
  _knownThresholds(id, d, learning) {
    const known = new Map();
    const add = (key, value) => {
      if (value == null) return;
      if (!known.has(key)) known.set(key, new Set());
      known.get(key).add(value);
    };
    for (const result of Object.values(learning?.slots || {})) {
      const before = result?.signature?.[id]?.thresholds || {};
      const proposed = result?.thresholds?.[id] || {};
      for (const key of Object.keys(before)) {
        add(key, before[key]);
        add(key, proposed[key]);
      }
    }
    for (const [key, value] of Object.entries(d.last_applied || {}))
      if (known.has(key)) add(key, value);
    return known;
  },

  // A live value the tuner never learned from, proposed or applied was
  // edited by hand after learning, whichever slot is being viewed.
  _manualEditWarningHtml(id, d, learning, name) {
    const known = this._knownThresholds(id, d, learning);
    const current = d.current_thresholds || {};
    const edited = [...known].some(
      ([key, values]) => current[key] != null && !values.has(current[key]),
    );
    return edited
      ? `<p class="notice" role="status">Manual gate edit: ${this._esc(name)}'s result is stale. Learn the set again.</p>`
      : "";
  },
};
