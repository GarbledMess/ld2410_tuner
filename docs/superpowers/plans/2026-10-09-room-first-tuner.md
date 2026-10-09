# Room-first Tuner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reorganise the tuner around rooms: a room-first panel tree, room-level labels with
per-radar attribution, linked-zone Learn/Apply owned by groups, and one room scoreboard —
without regressing current behaviour.

**Architecture:** New pure server modules (`rooms/layout.py`, `rooms/truth.py`,
`rooms/attribution.py`, `rooms/sets.py`, `rooms/room_labels.py`, `rooms/room_sources.py`)
feed the existing assessment/fitting/learning code through narrow seams; ungrouped radars
and one-group sets keep their current code paths. The panel replaces the Rooms card and
flat grid with `panel/layout.js`, reusing existing card, timeline and room-report code.

**Tech Stack:** Python 3.12 Home Assistant custom integration (HA stubbed in tests),
pytest + coverage, ruff, vanilla JS web component panel, Playwright browser regressions
(`tests/panel.cjs`, `tests/browser/*.cjs`), Prettier.

**Spec:** `docs/superpowers/specs/2026-10-09-room-first-tuner-design.md`

## Global Constraints

- No regressions: the full suite stays green at every commit; existing tests change only
  where the spec deliberately changes behaviour, called out in the commit message; never
  delete, skip or weaken a test.
- Ungrouped radars, single-radar groups and one-group learning sets behave exactly as
  today (numerical parity).
- Stored data from release 1.22.0 loads unchanged; new keys are additive:
  `room_labels`, `room_label_revision`, `room_presence_sources`; `room_learning` keyed by
  `set_id` (a one-group `set_id` equals its `group_id`).
- No new HA entities; no HA area/registry changes; no history row format, sampling
  cadence or retention changes.
- Each task leaves the panel usable and is independently releasable.
- Check suite (run before every commit that ends a task; all must pass):
  `python -m ruff check custom_components tools tests`,
  `python -m ruff format --check custom_components tools tests`,
  `python tools/check_complexity.py` (limit 10),
  `python tools/validate_package.py`,
  `python -m pytest --cov` (fail_under 85; do not lower overall coverage),
  `npm run format:check`, `PYTHON=<venv python> npm test`.
  On this Windows machine use `.venv/Scripts/python.exe` and keep LF line endings.
- Copy pinned by the spec: "Elsewhere in <area>", "Unassigned", "Not recording",
  "No radars in this group", "Area removed", "All radars", "How rooms and zones work",
  "Add zone", "Add manual group", "Linked: <A> + <B> · share <radar>",
  "Learn linked zones", "Review & apply", "Thresholds: learned with <set> ↑",
  "Seen by:", "Covers alone", "False-active", "Missed what it saw", "Radar on its own".

## Review Focus

1. **Restore area default must keep that area's room labels** (`remove_room` on an
   `area:` group restores membership; it is not a delete). Test in Task 6.
2. **A radar removed from HA while placed in zones or in a running set job** — layout
   skips it; the set job fails with "Zone labels, settings or membership changed; learn
   again" and other sets in the overnight pass still run. Tests in Tasks 2 and 15.
3. **Stale room-label edits from a second panel copy or tab** — rejected with "History
   labels changed. Refresh and select the period again." Test in Task 6.
4. **Linked set spanning two areas via an area-less group** — deterministic `set_id`,
   band rendered at top level, deleting one zone mid-learn marks the result stale.
   Tests in Tasks 13 and 16.
5. **A paused (recording disabled) member inside a learning set** — learn refuses with
   the existing "Enable recording for every member…" message naming the set; layout
   shows the radar under its HA area marked "Not recording". Tests in Tasks 2 and 15.

---

## File Structure

Server (`custom_components/ld2410_tuner/`):

| File | Responsibility |
| --- | --- |
| `rooms/layout.py` (new) | Pure: build the layout tree from areas/members/groups/devices |
| `rooms/sets.py` (new) | Pure: learning sets (connected components) and device → set |
| `rooms/room_labels.py` (new) | Room label storage/edit/validation; wraps generalised `history/labels.py` helpers |
| `rooms/truth.py` (new) | Pure: room truth precedence (own, implied, derived, sensor, estimate) |
| `rooms/attribution.py` (new) | Pure: per-episode, per-radar attribution and target rule |
| `rooms/room_sources.py` (new) | Group presence sources → `sensor` room labels |
| `rooms/evidence.py` | `room_label` delegates to `truth.room_truth` |
| `rooms/objective.py` | Preferences use attribution; `LinkedObjective` |
| `rooms/fitting.py` | `fit_set` over a learning set |
| `rooms/assessment.py` | Per-member `missed_seen_seconds`, `false_active_seconds` |
| `rooms/jobs.py`, `rooms/learning.py`, `rooms/application.py` | Keyed by `set_id`; signatures include room label revisions |
| `rooms/groups.py` | `for_device` → set resolution; `remove` keeps area labels |
| `calibration/constants.py` | Attribution constants |
| `presence/reference_training.py` | Room Empty live; attributed Present on rebuild |
| `presence/autolabelling.py` | Sample room sources each tick |
| `runtime/schedule.py` | Overnight per learning set |
| `runtime/websocket.py`, `runtime/coordinator.py` | New routes/bindings |
| `presentation/snapshots.py` | `layout`, `learning_sets`, `room_labels`, `room_attribution` |

Panel (`static/panel/`):

| File | Responsibility |
| --- | --- |
| `layout.js` (new, replaces `rooms.js`) | Tree render, placement reconciliation, group headers, editor |
| `room_timeline.js` (new) | Room label timeline, Seen-by chips, room sources, estimate feedback |
| `room_learning.js` | Set-level Learn/Apply, linked band |
| `room_report.js` | Role-in-zone card metrics, area zone score list |
| `view.js`, `card.js`, `history_graph.js`, `jobs.js` | `#layout`, placement keys, all-copies updates |

Tests: `tests/test_layout.py`, `tests/test_room_labels.py`, `tests/test_truth.py`,
`tests/test_attribution.py`, `tests/test_sets.py`, `tests/test_characterisation.py`
(+ `tests/fixtures/characterisation/*.json`), extend `tests/test_rooms.py`,
`tests/test_joint_learning.py`, `tests/test_references.py`; browser
`tests/browser/layout.cjs`, `tests/browser/room_labels.cjs`, extend `rooms.cjs`.

---

### Task 1: Characterisation baseline

**Files:**
- Create: `tests/test_characterisation.py`, `tests/fixtures/characterisation/` (JSON)
- Create: `tools/update_characterisation.py`

**Interfaces:**
- Produces: `golden(name: str, value) -> None` helper in `tests/test_characterisation.py`
  comparing `json.dumps(value, sort_keys=True)` (floats rounded to 9 places) with
  `tests/fixtures/characterisation/<name>.json`; fixtures are regenerated only by
  `tools/update_characterisation.py`, never by the test.

- [ ] **Step 1: Write characterisation tests** pinning current outputs on deterministic
  fixtures built with `test_rooms.member`/`labels` and `test_tuner.row_samples`:
  - `test_room_assessment_two_members` — `assessment.calculate` for
    `member("a",[40]*100+[0]*100,labels())`, `member("b",[3]*200,labels())`, 0–1200.
  - `test_joint_fit_examples` — `fitting.fit` for the four `test_joint_learning.example`
    inputs used there (thresholds, cost_before, cost_after, after.room).
  - `test_device_fit` — `fitting.fit_thresholds(row_samples({"g0_move":40},{"g0_move":5}), ["g0_move"])`
    full result minus timestamps.
  - `test_overnight_joint_attempt` — `nightly_learning` of both members after
    `schedule._run` in the `JointRuntimeTests.office` fixture (drop `started_at`,
    `finished_at`, `id`/`result_id`).
  - `test_reference_profile` — `reference_training.rebuild_human` profile summary for
    the `test_tuner` runtime fixture with one Present and one Empty human label.
- [ ] **Step 2: Generate fixtures** with `python tools/update_characterisation.py`, then
  run `python -m pytest tests/test_characterisation.py -v` — Expected: 5 passed.
- [ ] **Step 3: Run the check suite; commit** `test: pin current room/learn/reference behaviour`.

### Task 2: Layout tree builder

**Files:**
- Create: `custom_components/ld2410_tuner/rooms/layout.py`, `tests/test_layout.py`

**Interfaces:**
- Produces: `build(areas: dict[str,str], members: dict[str,dict], groups: dict[str,dict], device_ids: list[str], recording: dict[str,bool], device_areas: dict[str,str|None]) -> list[dict]`
  returning nodes `{"id","kind","name","group_id","devices","children","note"?}` with
  `kind` in `area|zone|rest|group|unassigned`, exactly as the spec §1 shape.
  `note` ∈ `"Area removed"`, `"Not recording"` (per device: `"not_recording": [ids]` on the
  leaf), `"No radars in this group"`.

- [ ] **Step 1: Write failing tests** (one per case; assert full returned structure):
  `test_no_areas_all_unassigned`, `test_area_without_zones_places_directly`,
  `test_area_with_zones_and_rest_leaf`, `test_overlapping_zones_repeat_radar`,
  `test_override_pulls_radar_from_other_area`, `test_area_less_group_top_level`,
  `test_deleted_area_zone_becomes_group_with_note`,
  `test_recording_disabled_falls_back_marked_not_recording`,
  `test_empty_group_keeps_header_with_note`, `test_unknown_group_member_skipped`,
  `test_unicode_names_sorted_by_name_then_id`, and
  `test_every_device_placed_and_only_known_devices` (property over all fixtures above:
  set of placed ids == set(device_ids)).
- [ ] **Step 2: Run** `python -m pytest tests/test_layout.py -v` — Expected: FAIL (module missing).
- [ ] **Step 3: Implement `build`** per spec §1 placement rules 1–3; sort key
  `(name.casefold(), id)`; area order before area-less groups before Unassigned.
- [ ] **Step 4: Run tests** — Expected: PASS. Run the check suite.
- [ ] **Step 5: Commit** `feat(rooms): build room-first layout tree`.

### Task 3: Snapshot exposes layout

**Files:**
- Modify: `presentation/snapshots.py:30-43`, `rooms/jobs.py:66-79`
- Test: `tests/test_rooms.py`

**Interfaces:**
- Consumes: `layout.build` (Task 2).
- Produces: `snapshot()["layout"]`; `room_jobs.snapshot` additionally returns the
  `device_areas`/`recording` maps it already reads (no second registry read).

- [ ] **Step 1: Failing test** `test_snapshot_layout_places_every_snapshot_device` in
  `GroupTests`: after `office()`, `snapshot["layout"]` contains both radars under
  `area:office` and set of placed ids == `set(snapshot["devices"])`.
- [ ] **Step 2: Run** — Expected: FAIL (`KeyError: 'layout'`).
- [ ] **Step 3: Implement**; keep `snapshot["rooms"]` unchanged.
- [ ] **Step 4: Run tests + check suite** — Expected: PASS.
- [ ] **Step 5: Commit** `feat(snapshot): include room layout`.

### Task 4: Panel renders the tree with repeated cards

**Files:**
- Create: `static/panel/layout.js` (tree render + placement reconciliation only)
- Modify: `static/panel/view.js:289-296,348-391` (`#grid` → `#layout`, `_draw`,
  `_captureDrafts`), `static/panel/card.js` (`data-placement`), `history_graph.js:100-160`,
  `jobs.js:40-70` (update every copy), `static/ld2410-tuner-panel.js` (mixin, `_cardCollapsed` per placement, `_lastEditedPlacement`)
- Test: create `tests/browser/layout.cjs`; register in `tests/panel.cjs`

**Interfaces:**
- Consumes: `snapshot.layout`.
- Produces: `_drawLayout()`; card elements carry `data-placement="<leaf id>|<device id>"`
  and `data-device-id`; `_placements(id) -> Element[]` returns every copy of a radar.
  The old Rooms card stays in place during this task.

- [ ] **Step 1: Failing browser tests** in `layout.cjs` with a fixture where radar `door`
  is in zones Desk and Sofa:
  - two `[data-device-id="door"]` cards, one under each zone leaf;
  - typing a history draft in the Sofa copy, triggering a poll, leaves the value in both
    copies (`_lastEditedPlacement` capture);
  - collapsing the Desk copy leaves the Sofa copy open;
  - selecting a chart range in one copy mirrors in the other;
  - a job status update renders in both copies;
  - snapshot without `layout` renders one "All radars" leaf with every radar;
  - at 390px width `document.documentElement.scrollWidth <= 390`.
- [ ] **Step 2: Run** `PYTHON=... npm test` — Expected: FAIL in `layout.cjs`.
- [ ] **Step 3: Implement**; reconciliation keyed by placement; charts fetched once per
  radar (`_maybeFetchChart` unchanged) and drawn into `_placements(id)`.
- [ ] **Step 4: Run full browser suite + check suite** — Expected: all PASS (existing
  `rendering.cjs`, `history_graph.cjs`, `jobs.cjs` unchanged and green).
- [ ] **Step 5: Commit** `feat(panel): render radars in the room tree`.

### Task 5: Group headers, editor and help move into the tree

**Files:**
- Modify: `static/panel/layout.js` (headers), delete `static/panel/rooms.js` content by
  moving `_roomHtml` → `_groupSummaryHtml(group)` + `_groupBodyHtml(id, group, rooms)`
  and `_wireRoom` → `_wireGroup(node, id)`; `view.js` (remove `#rooms` card, add collapsed
  "How rooms and zones work" `<details>` with the old explanatory text)
- Test: update `tests/browser/rooms.cjs` selectors to the tree (call out in commit)

**Interfaces:**
- Consumes: Task 4 tree; existing `_roomReportHtml`, `_roomLearningHtml`,
  `_wireRoomLearning`, `_roomEditorHtml`, `_wireRoomEditor`.
- Produces: header DOM `[data-group-id]` with `.group-summary` and `.group-body`;
  editor opened via `_openGroupEditor({group_id?, area_id?})` rendered inside the
  invoking node.

- [ ] **Step 1: Failing browser tests** (in `rooms.cjs`): assess from the Desk header;
  collapsed header shows "Needs reassessment" for a stale report; "Add zone" in Office
  prefills area `office`; saving Edit members moves the radar card to the new zone and
  keeps its open sections; Restore area default; no-areas fixture shows the
  "Assign recording radars…" empty text; editor draft survives a poll.
- [ ] **Step 2: Run** — Expected: FAIL.
- [ ] **Step 3: Implement**; defaults: nodes/leaves open, `.group-body` collapsed.
- [ ] **Step 4: Run full browser suite + check suite** — Expected: PASS.
- [ ] **Step 5: Commit** `feat(panel): room controls live in group headers` (note
  updated `rooms.cjs` selectors).

### Task 6: Room label storage and API

**Files:**
- Create: `rooms/room_labels.py`, `tests/test_room_labels.py`
- Modify: `history/labels.py` (extract owner-agnostic `apply_range(labels, start, end, state, source) -> list` and `replace_label(labels, original_start, original_end, start, end, state) -> list` from `label_history_range`/`edit_history_label`; device functions call them unchanged),
  `rooms/groups.py:remove` (area groups keep labels), `runtime/websocket.py`,
  `runtime/coordinator.py`, `presentation/snapshots.py`

**Interfaces:**
- Produces:
  - `label_room_range(runtime, group_id, start, end, state) -> {"ok", "room_labels", "revision"}`
  - `edit_room_label(runtime, group_id, label_start, label_end, start, end, state, revision) -> same`
  - `room_labels_for(runtime, group_id) -> list[dict]`
  - websocket routes `label_room_range`, `edit_room_label` (admin, `invalid_request`).
  - `snapshot["rooms"]["groups"][id]["labels"]` and `["label_revision"]`.
  - Every write bumps `runtime.data["room_label_revision"][group_id]` and marks
    `room_assessments[group_id]` stale.

- [ ] **Step 1: Failing tests:** `test_label_room_range_stores_and_bumps_revision`,
  `test_overlapping_room_labels_preserve_outer_parts` (same expectations as the device
  equivalent), `test_stale_room_edit_rejected` (message "History labels changed. Refresh
  and select the period again."), `test_unknown_group_rejected`,
  `test_restore_area_default_keeps_area_labels`, `test_remove_zone_drops_its_labels`,
  `test_device_label_behaviour_unchanged` (existing device label tests stay green — run
  `tests/test_history_editing.py`).
- [ ] **Step 2: Run** — Expected: FAIL.
- [ ] **Step 3: Implement** (refactor helpers first, run `test_history_editing.py`, then
  room functions).
- [ ] **Step 4: Run tests + check suite** — Expected: PASS; characterisation unchanged.
- [ ] **Step 5: Commit** `feat(rooms): room-level labels`.

### Task 7: Room truth precedence

**Files:**
- Create: `rooms/truth.py`, `tests/test_truth.py`
- Modify: `rooms/evidence.py:67-78` (`room_label` → `truth.room_truth`),
  `rooms/assessment.py`, `rooms/objective.py:34-50` (pass group context),
  `rooms/jobs.py:_context` (signature gains `"room_labels": {gid: revision}` for the group,
  its area parent and its zones)

**Interfaces:**
- Produces: `room_truth(context: TruthContext, timestamp: float) -> tuple[str, str, float] | None`
  where `TruthContext = {"own": reader, "parent": reader|None, "zones": [reader], "members": {id: evidence}, "sensor": reader, "estimate": callable|None}`;
  readers built by `truth.reader(labels, sources: set[str]) -> Callable[[float], tuple|None]`
  (`own`/`parent`/`zones` read `source == "manual"` labels; `sensor` reads
  `source == "sensor"` labels with their stored confidence). Returns
  `(state, "human"|"automatic", confidence)` per spec §4.2 order; an explicit human
  Unknown at levels 1–3 returns `None` without fallback.
- `truth.context(runtime_view, group_id, inputs) -> TruthContext`.

- [ ] **Step 1: Failing tests:** `test_own_label_beats_member_labels`,
  `test_zone_present_implies_area_present`, `test_area_empty_implies_zone_empty`,
  `test_area_less_group_has_no_implications`, `test_member_derivation_matches_old_rule`
  (table of the old `room_label` cases), `test_explicit_unknown_stops_fallback`,
  `test_human_outranks_sensor_and_estimate`.
- [ ] **Step 2: Run** — Expected: FAIL.
- [ ] **Step 3: Implement**; with no room labels the result equals the old
  `room_label` (levels 4 return `None` until Tasks 10–11).
- [ ] **Step 4: Run tests, `tests/test_characterisation.py`, check suite** — Expected:
  PASS with fixtures unchanged.
- [ ] **Step 5: Commit** `feat(rooms): room truth precedence`.

### Task 8: Attribution and its use in joint preferences

**Files:**
- Create: `rooms/attribution.py`, `tests/test_attribution.py`
- Modify: `calibration/constants.py`, `rooms/objective.py:75-89` (`_preferences`)

**Interfaces:**
- Constants: `ATTRIBUTION_TAIL_QUANTILE = 0.99`, `ATTRIBUTION_MARGIN = 3` (energy
  units above the baseline quantile), `ATTRIBUTION_SUSTAIN_SECONDS = 18`,
  `ATTRIBUTION_FULL_CONFIDENCE_SAMPLES = 100`; minimum baseline `MIN_REFERENCE` (20)
  from `presence/inference.py`.
- Produces:
  - `episodes(truth_at, times, gap=SAMPLE_SECONDS*2) -> list[tuple[float,float]]`
  - `baseline(rows, quiet_at) -> dict[str, list[int]]` (per-channel sorted values)
  - `attribute(rows, episode, base, radar_label_at) -> {"state": "yes"|"no"|"unknown", "confidence": float}`
    — human radar label over the episode decides (confidence 1.0); else "yes" when any
    channel has ≥ `ATTRIBUTION_SUSTAIN_SECONDS` of consecutive samples above
    `quantile(base, 0.99) + 3`; channels with < 20 baseline values are ignored; no usable
    channel → "unknown"; confidence = `min(1, n / 100)` of the deciding channel.
  - `targets(radar, group_truths, attributions) -> Callable[[float], "present"|"not_present"|None]`
    — union rule (spec §4.3).
- `Objective._preferences` assigns a member's row to Present only when attributed
  "yes"; Empty rows unchanged.

- [ ] **Step 1: Failing tests:** `test_quiet_member_not_attributed`,
  `test_brief_spike_below_sustain_not_attributed`, `test_sustained_rise_attributed`,
  `test_human_radar_label_decides`, `test_small_baseline_is_unknown`,
  `test_shared_radar_target_is_union_of_groups`, `test_deterministic_repeat`, and in
  `tests/test_joint_learning.py`
  `test_quiet_member_samples_do_not_shift_its_preference` (preferred threshold of the
  quiet radar equals the value computed from Empty rows only).
- [ ] **Step 2: Run** — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run tests + characterisation.** Fixtures where every member rises during
  Present must be unchanged; any characterisation change must be the quiet-member case
  only — inspect the diff, regenerate with `tools/update_characterisation.py`, and name
  it in the commit message.
- [ ] **Step 5: Run check suite; commit** `feat(rooms): attribute occupancy to radars`.

### Task 9: Attribution feeds per-radar evidence and references

**Files:**
- Modify: `calibration/evidence.py:collect_samples`, `presence/reference_training.py`
  (`observe`, `rebuild_human`), `rooms/room_labels.py` (rebuild members after edits)
- Test: `tests/test_references.py`, `tests/test_tuner.py`

**Interfaces:**
- Consumes: `truth.room_truth`, `attribution.targets` (Tasks 7–8).
- Produces: `room_targets(runtime, device_id) -> Callable[[float], str|None] | None`
  in `rooms/attribution.py` (None when the radar is in no multi-radar group).
  `collect_samples` uses a radar's own label first, else `room_targets`; ungrouped
  radars unchanged. Live `observe` adds human Empty when all the radar's groups are
  human-Empty; attributed Present enters only via `rebuild_human`, which room label
  edits now trigger for every member.

- [ ] **Step 1: Failing tests:** `test_room_empty_trains_member_empty_reference_live`,
  `test_room_present_unattributed_never_enters_present_reference`,
  `test_attributed_room_present_enters_on_rebuild`,
  `test_ungrouped_radar_collect_samples_unchanged` (equals characterisation
  `test_device_fit` input rows).
- [ ] **Step 2: Run** — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run tests, characterisation, check suite** — Expected: PASS, fixtures unchanged.
- [ ] **Step 5: Commit** `feat(learning): per-radar evidence from room labels`.

### Task 10: Room estimate

**Files:**
- Modify: `rooms/truth.py` (level-4 estimate), `rooms/room_labels.py`
  (`room_estimate_feedback`), websocket/coordinator
- Test: `tests/test_truth.py`

**Interfaces:**
- Produces: `estimate(member_guesses: list[tuple[str, float] | None]) -> tuple[str, str, float] | None`
  — Present: `1 - Π(1 - c)` over Present guesses; Empty only when every member is
  Empty, confidence `min`; result below `MIN_AUTO_CONFIDENCE` (0.55) → None; source
  `"automatic"`.
  `room_estimate_feedback(runtime, group_id, start, end, correct: bool)` writes a human
  room label (estimate state if correct, opposite if not) via `label_room_range`.

- [ ] **Step 1: Failing tests:** `test_noisy_or_present`, `test_empty_needs_all_members`,
  `test_mixed_is_none`, `test_below_floor_is_none`, `test_feedback_writes_human_label`.
- [ ] **Step 2–4:** Run (FAIL), implement, run tests + characterisation + check suite (PASS).
- [ ] **Step 5: Commit** `feat(rooms): room occupancy estimate`.

### Task 11: Room presence sources

**Files:**
- Create: `rooms/room_sources.py`
- Modify: `presence/sources.py` (extract `read(hass, config) -> dict` from
  `_read_sources`), `presence/autolabelling.py:_sample_devices` (then sample groups),
  websocket/coordinator, snapshots
- Test: `tests/test_sources.py`

**Interfaces:**
- Produces: `configure_room_sources(runtime, group_id, values) -> dict` (validated with
  `sources.validate`, stored in `runtime.data["room_presence_sources"][group_id]`);
  `sample_rooms(runtime, now)` appends/extends `source: "sensor"` room labels with the
  configured confidence, honouring start/end buffers via `source_buffer` keyed
  `f"room:{group_id}"`; route `configure_room_sources`. Configuring sources and writing
  sensor labels mark the group's assessment stale.

- [ ] **Step 1: Failing tests:** `test_room_source_present_extends_sensor_label`,
  `test_room_source_buffers_delay_start`, `test_mark_not_present_writes_empty`,
  `test_configuring_room_sources_marks_assessment_stale`,
  `test_device_sources_unchanged` (existing `test_sources.py` green).
- [ ] **Step 2–4:** Run (FAIL), implement, run tests + check suite (PASS).
- [ ] **Step 5: Commit** `feat(rooms): room presence sources`.

### Task 12: Panel room timeline, Seen-by chips, sources, conflicts

**Files:**
- Create: `static/panel/room_timeline.js`, `tests/browser/room_labels.cjs`
- Modify: `presentation/snapshots.py` (`room_attribution[group_id]`: latest 50 occupied
  episodes `{start,end,devices:{id:{state,confidence}}}`), `layout.js` (mount in
  `.group-body`), `static/panel/history.js` conflicts list (room conflicts), reuse
  `timeline.js`/`selection.js`
- Test: `tests/test_rooms.py` (snapshot), `room_labels.cjs`

**Interfaces:**
- Consumes: routes from Tasks 6, 10, 11; `snapshot.rooms.groups[id].labels`,
  `snapshot.room_attribution`.
- Produces: chips `[data-seen-by="<device>"]` with text "Seen by:" … "(high|low|unknown)"
  (high = confidence ≥ 0.8); toggling calls `label_history_range` on that radar for the
  episode with `present`/`not_present`.

- [ ] **Step 1: Failing tests:** snapshot `test_room_attribution_bounded_to_50`; browser:
  select a range on the Desk timeline and label Empty → `label_room_range` request;
  chip toggle sends the radar label request; room sources picker saves via
  `configure_room_sources`; conflict "room Empty vs radar Present" listed; "Was this
  right?" on an estimated stretch sends `room_estimate_feedback`; Remove on a zone with
  room labels asks for confirmation and sends nothing when cancelled.
- [ ] **Step 2–4:** Run (FAIL), implement, run full browser suite + check suite (PASS).
- [ ] **Step 5: Commit** `feat(panel): room labels and attribution`.

### Task 13: Learning sets

**Files:**
- Create: `rooms/sets.py`, `tests/test_sets.py`
- Modify: `rooms/groups.py` (`for_device` → `sets.for_device`; remove "multiple zones"
  errors — call out in commit, updating `test_joint_learning.test_zone_precedence_and_ambiguous_membership`
  expectations to the linked-set result), `presentation/snapshots.py`

**Interfaces:**
- Produces: `learning_sets(groups: dict) -> dict[str, {"group_ids": list, "device_ids": list}]`
  — multi-radar groups (area groups only if their area has no zones), union-find on shared
  radars, `set_id = "+".join(sorted(group_ids))`; `for_device(runtime, device_id) -> str | None`;
  `snapshot["learning_sets"]`.

- [ ] **Step 1: Failing tests:** `test_disjoint_zones_are_separate_sets`,
  `test_shared_radar_links_zones`, `test_area_with_zones_excluded`,
  `test_area_less_group_links_across_areas`, `test_single_radar_group_not_a_set`,
  `test_set_id_deterministic`, `test_one_group_set_id_equals_group_id`.
- [ ] **Step 2–4:** Run (FAIL), implement, run tests + check suite (PASS).
- [ ] **Step 5: Commit** `feat(rooms): linked learning sets`.

### Task 14: LinkedObjective and fit_set

**Files:**
- Modify: `rooms/objective.py` (add `LinkedObjective`), `rooms/fitting.py` (add
  `fit_set`; `fit` unchanged)
- Test: `tests/test_joint_learning.py`

**Interfaces:**
- Produces: `LinkedObjective(groups: dict[str, dict[str, member]], start, end)` exposing
  the `Objective` surface used by `search` (`current`, `preferred`, `radars`, `rank`,
  `costs`); `rank` sums per-group rank tuples element-wise, `costs` sums; `current` and
  `preferred` per radar (preferred = median of per-group preferences, rounded down).
  `fit_set(groups, start, end) -> dict` with `fit`'s keys plus
  `"groups": {gid: {"before", "after", "cost_before", "cost_after"}}`.

- [ ] **Step 1: Failing tests:** `test_one_group_set_matches_fit` (thresholds and costs
  equal `fitting.fit` on all four examples), `test_shared_radar_gets_single_value`,
  `test_linked_cost_is_sum_of_group_costs`.
- [ ] **Step 2–4:** Run (FAIL), implement, run tests + characterisation + check suite (PASS).
- [ ] **Step 5: Commit** `feat(rooms): fit linked zones together`.

### Task 15: Set-level jobs, Apply, automatic Apply, overnight

**Files:**
- Modify: `rooms/learning.py`, `rooms/jobs.py`, `rooms/application.py`,
  `runtime/schedule.py:_run`, `_share_joint_attempt`, `runtime/websocket.py`,
  `runtime/coordinator.py`, `calibration/jobs.py` (device learn → set)
- Test: `tests/test_joint_learning.py`, `tests/test_rooms.py`

**Interfaces:**
- Consumes: `sets.learning_sets`, `fitting.fit_set`.
- Produces: `learning.start_set(runtime, set_id, source="user")`, `start_device` resolves
  the device's set; `room_learning[set_id]`; routes `learn_set(set_id)`,
  `apply_set(set_id, result_id, source)`; `learn_room`/`apply_room` kept, resolving
  `group_id` → its set. `application.automatic` refuses with reason
  "A zone would get worse" when any `groups[gid].cost_after > cost_before`. Overnight
  iterates `learning_sets` once each, then ungrouped radars.

- [ ] **Step 1: Failing tests:** `test_learn_set_saves_all_members`,
  `test_apply_set_writes_union_lower_first`, `test_auto_apply_refuses_when_a_zone_worsens`,
  `test_overnight_visits_each_set_once` (shared radar in two zones learned once),
  `test_member_removed_mid_job_reports_changed_and_other_sets_continue`,
  `test_paused_member_refuses_with_existing_message`,
  `test_learn_room_alias_resolves_to_set`. Existing joint/overnight tests stay green.
- [ ] **Step 2–4:** Run (FAIL), implement, run full pytest + characterisation + check
  suite (PASS; `test_overnight_joint_attempt` fixture unchanged for the one-group case).
- [ ] **Step 5: Commit** `feat(rooms): learn and apply by learning set`.

### Task 16: Panel set-level Learn/Apply

**Files:**
- Modify: `static/panel/room_learning.js`, `layout.js` (linked band placement), `card.js`
  and `learning.js` (grouped cards hide Learn/Apply, show "Thresholds: learned with …")
- Test: extend `tests/browser/rooms.cjs`, `tests/browser/apply.cjs` (ungrouped unchanged)

**Interfaces:**
- Consumes: `snapshot.learning_sets`, routes `learn_set`/`apply_set`.

- [ ] **Step 1: Failing tests:** one-group set shows Learn together in its header;
  linked set shows band "Linked: Desk + Sofa · share Door radar" inside Office; a set
  spanning two areas renders its band at top level; grouped cards have no Learn/Apply
  and show the learned-with line; manual gate edit on a grouped card shows the stale
  warning; ungrouped Garage card keeps Learn/Apply (existing `apply.cjs` green).
- [ ] **Step 2–4:** Run (FAIL), implement, run full browser suite + check suite (PASS).
- [ ] **Step 5: Commit** `feat(panel): group-owned learn and apply`.

### Task 17: Per-member role metrics

**Files:**
- Modify: `rooms/assessment.py` (`devices[id]` adds `missed_seen_seconds`,
  `false_active_seconds`, `occupied_seconds`, `empty_seconds`); staleness of assessments
  on room label/source/attribution change (Task 6 already marks label writes)
- Test: `tests/test_rooms.py`

**Interfaces:**
- Produces: per-member fields above; `missed_seen_seconds` = seconds where the member is
  attributed "yes" and did not detect.

- [ ] **Step 1: Failing tests:** `test_missed_seen_counts_only_attributed_time`,
  `test_false_active_seconds_per_member`, `test_existing_report_fields_unchanged`
  (characterisation assessment equal on old keys).
- [ ] **Step 2–4:** Run (FAIL), implement, run tests + characterisation + check suite (PASS).
- [ ] **Step 5: Commit** `feat(rooms): per-radar role in zone`.

### Task 18: Panel scoreboard

**Files:**
- Modify: `static/panel/room_report.js`, `card.js`, `layout.js`
- Test: extend `tests/browser/rooms.cjs`

- [ ] **Step 1: Failing tests:** card under Desk shows "Covers alone 71%",
  "False-active 0.4%", "Missed what it saw …" from the fixture; same radar under Sofa
  shows Sofa's figures; "Radar on its own" detail holds the individual score; ungrouped
  card headline unchanged; Office header lists Desk and Sofa scores.
- [ ] **Step 2–4:** Run (FAIL), implement, run full browser suite + check suite (PASS).
- [ ] **Step 5: Commit** `feat(panel): room scoreboard`.

### Task 19: Docs, version, final verification

**Files:**
- Modify: `README.md` ("Rooms and independent zones", "Joint learning within a zone"),
  `docs/architecture.md` (new modules, panel `layout.js`/`room_timeline.js`, stored keys),
  `custom_components/ld2410_tuner/manifest.json` version `1.22.0` → `1.23.0`
- Test: `tests/test_release.py` (existing)

- [ ] **Step 1:** Update docs; bump version.
- [ ] **Step 2:** Run the complete check suite plus `python tools/release.py`; record
  pass counts and coverage in the commit message. Expected: all PASS, coverage ≥ 85% and
  not below the pre-plan 93.99%.
- [ ] **Step 3: Commit** `docs: room-first model; release 1.23.0`.
