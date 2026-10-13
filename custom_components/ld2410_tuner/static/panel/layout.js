// Room tree rendering. A radar can sit in several leaves, so every rendered
// card is a placement keyed "<leaf id>|<device id>". Shared radar state stays
// device-keyed; only whole-card collapse and node/leaf open state are per
// placement.

const FALLBACK_LEAF = {
  id: "all",
  kind: "all",
  name: "All radars",
  group_id: null,
  children: [],
};

// The group editor renders here when it is not inside a group header.
const FOOTER = "footer";

const EMPTY_ROOMS = { areas: {}, members: {}, groups: {} };

// Fields captured as drafts live in these sections.
const DRAFT_SECTIONS = '[data-section="history"], [data-section="training"]';

export function placementKey(leafId, deviceId) {
  return `${leafId}|${deviceId}`;
}

function deviceOf(key) {
  const text = String(key);
  return text.includes("|") ? text.slice(text.indexOf("|") + 1) : null;
}

// Card collapse. A placement key ("leaf|device") addresses one copy; a bare
// device id addresses every copy of that radar (the first-seen default and the
// older device-wide callers).
export class PlacementCollapse {
  constructor() {
    this._devices = new Set();
    this._overrides = new Map();
  }

  has(key) {
    if (this._overrides.has(key)) return this._overrides.get(key);
    const device = deviceOf(key);
    if (device !== null) return this._devices.has(device);
    if (!this._devices.has(key)) return false;
    // A bare id is collapsed only while no copy has been opened.
    for (const [placement, collapsed] of this._overrides)
      if (!collapsed && deviceOf(placement) === key) return false;
    return true;
  }

  add(key) {
    this._set(key, true);
  }

  delete(key) {
    this._set(key, false);
  }

  _set(key, collapsed) {
    if (deviceOf(key) !== null) {
      this._overrides.set(key, collapsed);
      return;
    }
    if (collapsed) this._devices.add(key);
    else this._devices.delete(key);
    for (const placement of [...this._overrides.keys()])
      if (deviceOf(placement) === key) this._overrides.delete(placement);
  }
}

function keyedChild(parent, attribute, key, create) {
  for (const child of parent.children)
    if (child.getAttribute(attribute) === key) return child;
  const element = create();
  element.setAttribute(attribute, key);
  return element;
}

function syncChildren(parent, desired) {
  for (const [index, child] of desired.entries())
    if (parent.childNodes[index] !== child)
      parent.insertBefore(child, parent.childNodes[index] || null);
  while (parent.childNodes.length > desired.length) parent.lastChild.remove();
}

function element(tag, className, html = "") {
  const node = document.createElement(tag);
  node.className = className;
  node.innerHTML = html;
  return node;
}

function setText(node, text) {
  if (node.textContent !== text) node.textContent = text;
}

export const panelLayout = {
  _layoutTree() {
    const layout = this._data?.layout;
    if (Array.isArray(layout)) return layout;
    return [
      { ...FALLBACK_LEAF, devices: Object.keys(this._data?.devices || {}) },
    ];
  },

  _placements(id) {
    return [
      ...(this.shadowRoot?.querySelectorAll(
        `[data-placement][data-device-id="${CSS.escape(id)}"]`,
      ) || []),
    ];
  },

  _cardCollapsed(placement) {
    return this._collapsed.has(placement);
  },

  _layoutIsOpen(key) {
    return this._layoutOpen.get(key) ?? true;
  },

  // Remember which copy of a radar was last edited so drafts are captured
  // from it rather than from whichever copy comes last in the page.
  _rememberEditedPlacement(event) {
    const card = event.target.closest?.("[data-placement]");
    if (card && event.target.closest(DRAFT_SECTIONS))
      this._lastEditedPlacement.set(
        card.dataset.deviceId,
        card.dataset.placement,
      );
  },

  _wireLayoutEvents(root) {
    for (const type of ["input", "change", "click", "pointerdown", "keydown"])
      root.addEventListener(
        type,
        (event) => this._rememberEditedPlacement(event),
        true,
      );
  },

  _drawLayout() {
    const root = this.shadowRoot.querySelector("#layout");
    this._captureDrafts(root);
    const devices = this._data?.devices || {};
    this._renderedGroups = new Set();
    this._placedSets = new Set();
    if (!Object.keys(devices).length) {
      // Groups can still be created before any radar is discovered.
      const notice = keyedChild(root, "data-layout-empty", "empty", () =>
        element(
          "div",
          "card",
          "No LD2410 gate energy entities were discovered.",
        ),
      );
      syncChildren(root, [notice, this._layoutFooterElement(root)]);
      return;
    }
    const cards = new Map(
      Array.from(root.querySelectorAll("[data-placement]"), (card) => [
        card.dataset.placement,
        card,
      ]),
    );
    const nodes = this._layoutTree().map((node) =>
      this._layoutNodeElement(root, node, devices, cards),
    );
    // A linked set spanning areas (or outside any area) has its band on top.
    const bands = this._linkedSets()
      .filter((set) => !this._placedSets.has(set.id))
      .map((set) => this._setBandElement(root, set));
    syncChildren(root, [...bands, ...nodes, this._layoutFooterElement(root)]);
    for (const [id, device] of Object.entries(devices))
      this._maybeFetchChart(id, device);
  },

  _layoutDetails(parent, attribute, key, className, heading) {
    const node = keyedChild(parent, attribute, key, () => {
      const details = element(
        "details",
        className,
        `<summary><${heading}></${heading}></summary>`,
      );
      details.open = this._layoutIsOpen(key);
      // The toggle event is queued, so a poll could redraw first and undo a
      // click; record the new state when the summary is activated as well.
      details.firstElementChild.addEventListener("click", () =>
        this._layoutOpen.set(key, !details.open),
      );
      details.addEventListener("toggle", () =>
        this._layoutOpen.set(key, details.open),
      );
      return details;
    });
    if (node.open !== this._layoutIsOpen(key))
      node.open = this._layoutIsOpen(key);
    return node;
  },

  _layoutNodeElement(root, node, devices, cards) {
    if (node.kind === "all")
      return this._layoutLeafElement(root, node, devices, cards);
    const details = this._layoutDetails(
      root,
      "data-node-id",
      node.id,
      "layout-node",
      "h2",
    );
    const summary = details.firstElementChild;
    setText(summary.firstElementChild, node.name);
    const children = node.children || [];
    const parts = [
      summary,
      ...this._groupHeaderElement(details, node),
      ...this._nodeBandElements(details, node),
    ];
    if ((node.devices || []).length || !children.length || node.note)
      parts.push(this._layoutDirectElement(details, node, devices, cards));
    // "Elsewhere" has no group; its cards show their role in the area's.
    for (const child of children)
      parts.push(
        this._layoutLeafElement(details, child, devices, cards, {
          roleGroup: child.kind === "rest" ? node.group_id : child.group_id,
        }),
      );
    syncChildren(details, parts);
    return details;
  },

  // Bands for linked sets whose zones all sit in this node.
  _nodeBandElements(parent, node) {
    const groups = new Set(
      [node, ...(node.children || [])].map((item) => item.group_id),
    );
    return this._linkedSets()
      .filter((set) => set.group_ids.every((key) => groups.has(key)))
      .map((set) => {
        this._placedSets.add(set.id);
        return this._setBandElement(parent, set);
      });
  },

  _setBandElement(parent, set) {
    const band = keyedChild(parent, "data-set-id", set.id, () =>
      element("section", "linked-set"),
    );
    this._updateHtml(band, this._linkedBandHtml(set));
    this._wireSetLearning(band, {
      id: set.id,
      learning: set.learning,
      name: this._setName(set),
      count: set.device_ids.length,
    });
    return band;
  },

  _layoutDirectElement(parent, node, devices, cards) {
    const direct = keyedChild(parent, "data-leaf-id", node.id, () =>
      element("div", "layout-direct"),
    );
    this._layoutLeafBody(direct, [], node, devices, cards);
    return direct;
  },

  _layoutLeafElement(parent, leaf, devices, cards, options = {}) {
    const details = this._layoutDetails(
      parent,
      "data-leaf-id",
      leaf.id,
      "layout-leaf",
      "h3",
    );
    const summary = details.firstElementChild;
    setText(summary.firstElementChild, leaf.name);
    this._layoutLeafBody(
      details,
      [summary, ...this._groupHeaderElement(details, leaf)],
      leaf,
      devices,
      cards,
      options.roleGroup,
    );
    return details;
  },

  _layoutLeafBody(
    container,
    parts,
    leaf,
    devices,
    cards,
    roleGroup = leaf.group_id,
  ) {
    if (leaf.note) {
      const note = keyedChild(container, "data-layout-note", "note", () =>
        element("p", "layout-note muted"),
      );
      setText(note, leaf.note);
      parts.push(note);
    }
    const grid = keyedChild(container, "data-layout-grid", "grid", () =>
      element("div", "grid"),
    );
    const notRecording = new Set(leaf.not_recording || []);
    const placed = (leaf.devices || [])
      .filter((id) => id in devices)
      .map((id) => {
        const key = placementKey(leaf.id, id);
        return this._createCard(id, devices[id], cards.get(key), key, {
          notRecording: notRecording.has(id),
          groupId: roleGroup,
        });
      });
    syncChildren(grid, placed);
    parts.push(grid);
    syncChildren(container, parts);
  },

  // ---- Group headers -----------------------------------------------------
  // Every node or leaf whose group exists gets a collapsible header: the
  // collapsed line summarises the group, the body holds its controls, report,
  // joint learning and (when invoked here) the group editor.

  _roomsData() {
    return this._data?.rooms || EMPTY_ROOMS;
  },

  _groupHeaderElement(parent, node) {
    const rooms = this._roomsData();
    const id = node.group_id;
    const group = id ? rooms.groups[id] : null;
    if (!group) return [];
    this._renderedGroups?.add(id);
    const header = keyedChild(parent, "data-group-id", id, () => {
      const details = element(
        "details",
        "group-header",
        '<summary class="group-summary"></summary><div class="group-body"></div>',
      );
      const remember = (open) => {
        if (open) this._groupOpen.add(id);
        else this._groupOpen.delete(id);
      };
      // The toggle event is queued, so a poll could redraw first and undo a
      // click; record the new state when the summary is activated as well.
      details.firstElementChild.addEventListener("click", () =>
        remember(!details.open),
      );
      details.addEventListener("toggle", () => remember(details.open));
      return details;
    });
    if (header.open !== this._groupOpen.has(id))
      header.open = this._groupOpen.has(id);
    this._updateHtml(
      header.firstElementChild,
      this._groupSummaryHtml(group, node),
    );
    // Room timeline fields are read back first so a poll never resets them.
    this._captureRoomFields(header.lastElementChild);
    this._updateHtml(
      header.lastElementChild,
      this._groupBodyHtml(id, group, rooms),
    );
    this._wireGroup(header, id);
    return [header];
  },

  _groupKindLabel(group, rooms) {
    if (group.kind === "area") return "Whole area";
    return group.area_id in rooms.areas ? "Zone" : "Manual group";
  },

  _groupSummaryHtml(group, node) {
    const rooms = this._roomsData();
    const report = group.assessment || {};
    const count = group.device_ids.length;
    const zones = (node.children || [])
      .filter((child) => child.kind === "zone" && rooms.groups[child.group_id])
      .map(
        (child) =>
          `${this._esc(child.name)} ${this._scoreText(rooms.groups[child.group_id].assessment?.room)}`,
      );
    const zoneList = zones.length
      ? `<span class="group-zones"> · Zones: ${zones.join(", ")}</span>`
      : "";
    return `<b>${this._esc(group.name)}</b> · ${this._groupKindLabel(group, rooms)} · ${this._scoreText(report.room)}${this._roomAssessmentMarker(report)} · ${count} radar${count === 1 ? "" : "s"}${zoneList}`;
  },

  _roomAssessmentMarker(report) {
    if (report.state === "running") return " · Assessing…";
    if (report.state === "stale") return " · Needs reassessment";
    return "";
  },

  _groupBodyHtml(id, group, rooms) {
    const report = group.assessment || {};
    const busy = report.state === "running";
    const names = group.device_ids
      .map((key) => rooms.members[key]?.name || "Unavailable radar")
      .join(", ");
    const area =
      rooms.areas[group.area_id] ||
      (group.area_id ? "Area removed" : "Manual group");
    const reason =
      report.reason || (report.state === "pending" ? "No assessment yet" : "");
    const status = busy
      ? "Assessing recorded coverage. You can leave and return."
      : reason;
    const removeLabel =
      group.kind === "area" ? "Restore area default" : "Remove group";
    const removeButton = group.automatic
      ? ""
      : `<button data-room-action="remove">${removeLabel}</button>`;
    const addZone =
      group.kind === "area" && group.area_id in rooms.areas
        ? '<button data-room-action="add-zone">Add zone</button>'
        : "";
    const editor =
      this._roomEditorAt === id
        ? `<div class="room-editor">${this._roomEditorHtml(rooms)}</div>`
        : "";
    return `<p class="muted">${this._esc(area)} · ${this._esc(names)}${group.automatic ? " · Uses recording radars in this area" : " · Manual membership"}</p>
      <div class="chart-controls"><label>History <select data-room-hours><option value="24">24 hours</option><option value="72">3 days</option><option value="168">7 days</option></select></label>
      <button data-room-action="assess" ${busy ? 'disabled class="is-busy"' : ""}>Assess current settings</button>
      <button data-room-action="edit">Edit members</button>${removeButton}${addZone}</div>
      <div role="status" class="muted ${busy ? "is-busy" : ""}">${this._esc(status)}</div>
      ${editor}${this._roomReportHtml(report, rooms.members)}${this._roomTimelineHtml(id, group)}${this._roomSourcesHtml(id, group)}${this._groupLearningHtml(id, group, rooms)}`;
  },

  // Removing a zone or manual group deletes its room labels; restoring an
  // area default keeps them.
  _confirmRemove(group) {
    const count = (group.labels || []).length;
    return (
      group.kind === "area" ||
      !count ||
      confirm(
        `Remove ${group.name}? Its room labels (${count}) will be deleted. Radar labels are kept.`,
      )
    );
  },

  _wireGroup(header, id) {
    const group = this._roomsData().groups[id];
    this._wireRoomLearning(header, id, group);
    this._wireRoomTimeline(header.lastElementChild, id, group);
    const hours = header.querySelector("[data-room-hours]");
    this._roomHours ||= new Map();
    hours.value = this._roomHours.get(id) || "24";
    hours.onchange = () => this._roomHours.set(id, hours.value);
    header.querySelector('[data-room-action="edit"]').onclick = () =>
      this._openGroupEditor({ group_id: id });
    const addZone = header.querySelector('[data-room-action="add-zone"]');
    if (addZone)
      addZone.onclick = () => this._openGroupEditor({ area_id: group.area_id });
    for (const action of ["assess", "remove"]) {
      const button = header.querySelector(`[data-room-action="${action}"]`);
      if (button)
        button.onclick = () =>
          this._action(button, async () => {
            if (action === "remove" && !this._confirmRemove(group)) return;
            await this._call(
              action === "assess" ? "assess_room" : "remove_room",
              {
                group_id: id,
                ...(action === "assess" ? { hours: Number(hours.value) } : {}),
              },
            );
            await this._load(true);
          });
    }
    this._wireRoomEditor(header);
  },

  _layoutFooterElement(root) {
    const rooms = this._roomsData();
    const footer = keyedChild(root, "data-layout-footer", FOOTER, () =>
      element("div", "layout-footer"),
    );
    const empty = Object.keys(rooms.groups).length
      ? ""
      : '<p class="muted">Assign recording radars to Home Assistant areas, or create a group below.</p>';
    // A draft whose header is gone (group removed, layout changed) shows here.
    const editor =
      this._roomDraft && !this._renderedGroups?.has(this._roomEditorAt)
        ? `<div class="room-editor">${this._roomEditorHtml(rooms)}</div>`
        : "";
    this._updateHtml(
      footer,
      `${empty}<button data-room-action="add-group">Add manual group</button>${editor}`,
    );
    footer.querySelector('[data-room-action="add-group"]').onclick = () =>
      this._openGroupEditor({});
    this._wireRoomEditor(footer);
    return footer;
  },

  // Open the group editor inside the node that invoked it: the group's own
  // header for Edit members, the area's header for Add zone, else the footer.
  _openGroupEditor({ group_id, area_id } = {}) {
    const rooms = this._roomsData();
    const group = group_id ? rooms.groups[group_id] : null;
    this._roomDraft = group
      ? {
          group_id,
          name: group.name,
          area_id: group.area_id || "",
          device_ids: [...group.device_ids],
        }
      : { name: "", area_id: area_id || "", device_ids: [] };
    const areaGroup = Object.keys(rooms.groups).find(
      (key) =>
        rooms.groups[key].kind === "area" &&
        rooms.groups[key].area_id === area_id,
    );
    this._roomEditorAt = group ? group_id : (area_id && areaGroup) || FOOTER;
    if (this._roomEditorAt !== FOOTER) this._groupOpen.add(this._roomEditorAt);
    this._drawLayout();
    this.shadowRoot.querySelector("#layout .room-editor input")?.focus();
  },

  // Open a group's header and every enclosing node, so it can be scrolled to.
  _revealGroup(id) {
    this._groupOpen.add(id);
    this._drawLayout();
    const header = this.shadowRoot.querySelector(
      `#layout [data-group-id="${CSS.escape(id)}"]`,
    );
    if (!header) return null;
    this._openAncestors(header);
    header.open = true;
    return header;
  },

  // Open every node and leaf enclosing an element, remembering the state.
  _openAncestors(target) {
    for (
      let node = target.parentElement?.closest("details");
      node;
      node = node.parentElement?.closest("details")
    ) {
      this._layoutOpen.set(node.dataset.leafId ?? node.dataset.nodeId, true);
      node.open = true;
    }
  },

  _roomEditorHtml(rooms) {
    const draft = this._roomDraft;
    if (!draft) return "";
    const areaOptions = Object.entries(rooms.areas)
      .map(
        ([id, name]) =>
          `<option value="${this._esc(id)}" ${draft.area_id === id ? "selected" : ""}>${this._esc(name)}</option>`,
      )
      .join("");
    const members = Object.entries(rooms.members)
      .map(
        ([id, item]) =>
          `<label><input type="checkbox" data-room-member="${this._esc(id)}" ${draft.device_ids.includes(id) ? "checked" : ""}> ${this._esc(item.name)} <span class="muted">${this._esc(rooms.areas[item.area_id] || "No area")}${item.recording_enabled ? "" : " · recording paused"}</span></label>`,
      )
      .join("");
    return `<form class="pattern-comparison"><h3>${draft.group_id ? "Edit group" : "Add zone or manual group"}</h3>
      <div class="history-fields"><label>Name<input name="room-name" required maxlength="80" value="${this._esc(draft.name)}"></label>
      <label>Area<select name="room-area" ${draft.group_id?.startsWith("area:") ? "disabled" : ""}><option value="">No area / custom group</option>${areaOptions}</select></label></div>
      <fieldset class="room-members"><legend>Radars covering this zone</legend>${members}</fieldset>
      <p class="muted">Select only radars that can represent this zone. A radar shared with another zone contributes to both; grouping cannot distinguish which side of its coverage triggered. Assigning an area here organizes this group; it does not move devices in Home Assistant or change whole-area membership.</p>
      <button type="submit">Save group</button> <button type="button" data-room-action="cancel">Cancel</button></form>`;
  },

  _wireRoomEditor(container) {
    const form = container.querySelector(".room-editor form");
    if (!form) return;
    const capture = () => {
      this._roomDraft = {
        ...this._roomDraft,
        name: form.elements["room-name"].value,
        area_id: form.elements["room-area"].value,
        device_ids: [
          ...form.querySelectorAll("[data-room-member]:checked"),
        ].map((input) => input.dataset.roomMember),
      };
    };
    form.oninput = capture;
    form.onchange = capture;
    form.querySelector('[data-room-action="cancel"]').onclick = () => {
      this._roomDraft = null;
      this._roomEditorAt = null;
      this._drawLayout();
    };
    form.onsubmit = (event) => {
      event.preventDefault();
      capture();
      const button = form.querySelector('[type="submit"]');
      void this._action(button, async () => {
        await this._call("configure_room", {
          ...this._roomDraft,
          area_id: this._roomDraft.area_id || null,
        });
        this._roomDraft = null;
        this._roomEditorAt = null;
        await this._load(true);
      });
    };
  },
};
