# Room-first tuner: layout, labels, learning and scores

Status: design approved in conversation, 2026-10-09. One spec, one implementation plan.

## Problem

The panel treats rooms and radars as separate worlds. A "Rooms and zones" card sits
above a flat grid of radar cards. Device Learn silently reroutes to a joint job and
Apply jumps to a review elsewhere. Room scores and radar scores are different measures
shown in different places. Labels are per radar, so room occupancy must be entered on
every member. Learning and automatic guessing assume that an occupied room means every
member radar should detect, which is false: a person is often seen by one radar, or
several, but not all.

## Goals

1. The page is organised by rooms, respecting overlap: areas, zones within areas,
   manual groups without an area, radars shared by several zones, and radars in no
   room.
2. Learn/Apply for grouped radars lives on the group, with overlapping zones learned
   together so a shared radar gets one value that serves every zone.
3. One scoreboard: the room score is the headline; radar cards show their role in that
   room.
4. Occupancy is labelled per room. Learning and guessing infer which radars actually
   saw the presence (attribution) instead of assuming all members did.

## Non-goals

- No new Home Assistant entities, no changes to HA areas or the device registry.
- No change to history row format, sampling cadence, retention budget, or the
  fitting search's iteration order and tie-breaking for a single group.
- No claim of field accuracy; existing caveats in the README stay.

## Definitions

- **Group**: an entry from `rooms/groups.definitions` — an area group (`area:<id>`,
  registry-derived or with edited members), a zone (manual group with `area_id`), or an
  area-less manual group.
- **Leaf**: a place where radar cards render (a zone, an area's "Elsewhere" remainder,
  an area with no zones, an area-less group, Unassigned).
- **Placement**: one rendered card, keyed `"<leaf id>|<device id>"`.
- **Learning set**: connected component of multi-radar groups linked by shared radars.
  Area groups take part only when the area has no zones.
- **Room truth**: Present / Empty / Unknown for a group at time t (section 4.2).
- **Attribution**: whether a radar saw a given occupied episode (yes / no / unknown,
  with confidence).

## 1. Layout tree (server)

New pure module `rooms/layout.py`, `build(areas, members, groups, device_ids)`,
returning `snapshot["layout"]`. Built in `presentation/snapshots.py` from the same
values `room_jobs.snapshot` already computed (no second registry read). The existing
`snapshot["rooms"]` stays.

Top-level node order (by name, then id, within each kind):

1. Areas: every HA area with a radar, an area group, or a zone organised under it.
2. Area-less manual groups. A zone whose area no longer exists is treated as one,
   with an "Area removed" note.
3. Unassigned (only when non-empty).

Inside an area: one leaf per zone organised under it; an "Elsewhere in <area>" leaf for
area-group members in none of its zones; when the area has no zones its radars sit
directly in the area node. The area node carries the area `group_id`.

A radar is placed:

1. in every zone and area-less group whose members include it;
2. in its area's Elsewhere/direct leaf if it is in that area group's members and in none
   of that area's zones (follows "Edit members" overrides);
3. otherwise (e.g. recording disabled, which removes it from area defaults) in its HA
   area's leaf marked "Not recording", else in Unassigned.

Invariant: every device in `snapshot["devices"]` has at least one placement, and only
those devices are placed. Group members that no longer exist are skipped (the header's
membership line keeps showing "Unavailable radar"). Empty groups keep their header with
"No radars in this group".

Shape:

```json
[{"id": "area:office", "kind": "area", "name": "Office", "group_id": "area:office",
  "devices": [],
  "children": [
    {"id": "zone:abc", "kind": "zone", "name": "Desk", "group_id": "zone:abc",
     "devices": ["desk", "door"]},
    {"id": "area:office/rest", "kind": "rest", "name": "Elsewhere in Office",
     "group_id": null, "devices": ["hall"]}]},
 {"id": "zone:def", "kind": "group", "name": "Open-plan", "group_id": "zone:def",
  "devices": ["kitchen", "lounge"], "children": []},
 {"id": "unassigned", "kind": "unassigned", "name": "Unassigned", "group_id": null,
  "devices": ["garage"], "children": []}]
```

Learning sets (section 5) are added to the snapshot as
`snapshot["learning_sets"] = {set_id: {"group_ids": [...], "device_ids": [...]}}`.

## 2. Panel rendering

- `#grid` and the Rooms card are replaced by `#layout`, rendered from
  `snapshot.layout`. `panel/rooms.js` becomes `panel/layout.js` (tree, headers,
  editor); room report/learning modules are reused.
- Top-level nodes are collapsible `<details>`; leaves are sub-sections with a heading
  and the existing card grid.
- Cards reuse `_createCard`, add `data-placement`, keep `data-device-id`. Redraw
  reconciliation is keyed by placement.
- **Shared per radar** (already device-keyed maps): sections, drafts, chart data and
  range selection, saved-result selection, busy/job state. Changes redraw into every
  copy. Charts are fetched once per radar and drawn into each visible copy.
- **Per placement**: whole-card collapse; node/leaf open state (replaces `_roomOpen`).
- Drafts are captured from the copy last typed in, not the last copy in DOM order. The
  existing "hold redraw while editing" rule stays.
- Single-card lookups (`history_graph.js`, `jobs.js`) update every copy.
- If `layout` is missing (old server during an update), render one flat "All radars"
  leaf.
- Defaults: nodes and leaves open; group overview blocks collapsed.

## 3. Group headers and editing

Every node/leaf with a `group_id` has a header.

- Collapsed line: name · Whole area / Zone / Manual group · room score · marker
  (Assessing… / Needs reassessment) · radar count.
- Expanded overview block (above the leaf's cards; for an area, above its zones):
  membership line, history window, Assess, Edit members, Restore default / Remove,
  status, assessment report, room label timeline (4.1), room presence sources (4.5),
  and Learn/Apply for one-group learning sets (5).
- "Elsewhere" and Unassigned have plain headings and no controls.
- Each area header has **Add zone** (editor prefilled with that area); one **Add manual
  group** at the end of the layout. **Edit members** opens the editor inline in the
  group's overview. Saving reloads the layout.
- The explanatory text from the old Rooms card moves into one collapsed "How rooms and
  zones work" panel at the top of the layout, rewritten for room labels and
  attribution.
- Area headers for areas with zones show the whole-area score (assessment only) and a
  compact list of zone scores.

## 4. Room labels, attribution and guessing

### 4.1 Room labels

- Storage: `runtime.data["room_labels"][group_id] = [{start, end, state, source,
  confidence?}]` with `room_label_revision[group_id]`. `source` is `manual` or
  `sensor` (4.5). Validation, overlap preservation and stale-revision rejection reuse
  `history/labels.py` helpers, generalised to accept a label list owner.
- Edited on a timeline in the group overview, reusing the radar cards' timeline and
  range-selection components.
- A radar in no group: its own card timeline remains its room; behaviour unchanged.
- Removing a group removes its room labels; the Remove button asks for confirmation
  when the group has any.

### 4.2 Room truth

For group G at time t, first match wins:

1. G's own human label.
2. Implied human labels: a zone Present ⇒ its area Present; an area Empty ⇒ every zone
   organised under it Empty. Nothing flows across area-less groups.
3. Derived from member radar labels (current `evidence.room_label` rule).
4. Room automatic evidence: room sensor labels (4.5), then the room estimate (4.4).

An explicit human Unknown at levels 1–3 stops fallback. Human evidence always
outranks automatic.

### 4.3 Attribution

Radar labels are read as "should this radar detect here": Present = the room was
occupied and this radar saw it; Empty = this radar should stay quiet. Existing labels
need no migration.

New pure module `rooms/attribution.py`. For each occupied episode of each group (a
maximal run of Present room truth, split at recording gaps), for each member r:

1. A human radar label over the episode decides it (Present → yes, Empty → no,
   confidence 1).
2. Otherwise compare r's readings with **r's empty baseline**: r's samples where every
   group r belongs to has Empty room truth, plus r's own Empty labels. Per gate
   channel, r is attributed when the channel is sustained above the baseline's upper
   tail. Confidence scales with baseline support. Tail quantile, margin and
   "sustained" duration are constants in `calibration/constants.py`, chosen in the plan
   using `tools/` replays against recorded data.
3. With baseline support below `MIN_REFERENCE` the result is **unknown**.

Shared radars: r is a detection target at t if any of its groups is Present with r
attributed; it is a quiet target only when every one of its groups is Empty. Otherwise
(occupied but not attributed, or unknown) r has no per-radar target at t.

Attribution is deterministic, computed during learn/assess/snapshot, and cached per
(group, room label revision, member label revisions, history block set).

Uses:

- Per-radar Present evidence for the learner, `separation.gate_preference`, and
  reference training (`presence/reference_training.py`) uses attributed samples only;
  Empty evidence uses quiet-target samples.
- The joint rule "a miss counts only when no member detects" is unchanged.
- "Missed what it saw" in scores (section 6).

UI: each occupied period on a group timeline shows "Seen by: <radar> (high/low/
unknown) …". Tapping a chip toggles it and writes the corresponding radar label for
that period (bumping that radar's label revision).

Conflicts listed in the existing "Reviewing conflicting labels" view: room Empty vs a
radar Present label; zone Present inside an area labelled Empty; room human label vs
a contradicting room sensor label.

### 4.4 Room estimate

Derived on demand from recorded member guesses (the code and confidence already in
each history row); no new per-sample storage. Per time cell:

- Present: noisy-OR of member Present confidences.
- Empty: only when every member is confidently Empty; confidence = minimum.
- Otherwise: none.

The existing `MIN_AUTO_CONFIDENCE` floor applies. The per-radar estimator
(`presence/inference.py`) is unchanged; its references now come from attributed
evidence, so it learns that radar's own presence.

Feedback on a room estimate ("Was this right?") writes a human room label for that
stretch, bounded like today's device feedback.

### 4.5 Room presence sources

Cameras, boolean entities and Bermuda can be attached to a group in its overview.
Same schema, validation, buffers and confidence as device sources (`presence/
sources.py`, `source_buffer.py`), keyed by group. Output is stored as `sensor` room
labels with confidence. Device-level sources stay, meaning "this radar's coverage is
occupied" (radar-level evidence).

## 5. Learn and Apply

- Learning sets: connected components of multi-radar groups sharing a radar (area
  groups only when the area has no zones). `set_id` = sorted group ids joined by `+`.
  Single-radar groups and ungrouped radars learn individually as today.
- `rooms/fitting.py` gains `LinkedObjective`: one `Objective` per group on its own room
  truth and attribution; cost = sum of group costs; the existing deterministic
  coordinate search runs over the union of member radars. A one-group set is
  numerically identical to today's joint fit.
- `room_learning` results are keyed by `set_id`; the existing signature checks mark
  them stale when membership, labels (room or radar), attribution corrections,
  thresholds or timing change.
- UI: one-group set → Learn together / Review & apply in that group's overview.
  Linked set → a band inside the area node (top level if it spans areas): "Linked:
  Desk + Sofa · share Door radar" with Learn linked zones / Review & apply; zone
  headers show their own before → after and link to the band.
- Member radar cards have no Learn/Apply buttons; they show "Thresholds: learned with
  <set> ↑" (current, proposed, last applied). Manual gate edits remain, with a warning
  that the set's result will become stale.
- Apply writes the whole set with the existing paced writer, lower-before-higher
  ordering, guards and Current/Previous rotation only when every member succeeds.
- Automatic Apply: every member permits it, total cost improves, and **no group's
  cost increases**.
- Overnight learning runs each learning set once and shares the outcome with its
  members (replaces `_share_joint_attempt`).
- Websocket `start_learning(device_id)` resolves to the device's learning set
  (deterministic). The "multiple zones" / ambiguous-group errors are removed.

## 6. Scores

- Group header: room score (existing 0–100 time-cost formula) is the only headline. A
  linked band shows each zone's score and before → after.
- Radar card under a group shows its role in that group, from the assessment's
  per-member results:
  - Covers alone: `exclusive_presence_seconds` ÷ occupied seconds.
  - False-active: share of Empty time this radar detected.
  - Missed what it saw: attributed occupied time not detected.
- "Radar on its own" detail keeps the individual learner score, labelled as a
  different, non-comparable measure.
- Ungrouped radars keep the individual learner score as headline.
- Room label edits, attribution corrections and room source changes mark affected
  groups' assessments stale through the existing signature mechanism.

## 7. Compatibility and data

- New stored keys: `room_labels`, `room_label_revision`, `room_presence_sources`;
  `room_learning` re-keyed by `set_id` (old one-group entries keep their `group_id`
  key, which equals the one-group `set_id`).
- No change to device label storage or history rows.
- Panel and server ship together; the panel falls back gracefully without `layout`.
- Bump `manifest.json` version (currently 1.22.0) so the release job publishes.

## 8. Testing

Python (pytest, coverage ≥ 85%, no lowered coverage):

- `layout.build`: no areas; area with/without zones; overlapping zones; override
  pulling in another area's radar; area-less groups; deleted area; recording disabled;
  empty groups; unknown members; unicode names; deterministic order; placement
  invariant.
- Room truth precedence including implications and explicit Unknown.
- Attribution: human radar labels decide; sustained vs brief rises; insufficient
  baseline → unknown; shared radar union rule; determinism.
- Room estimate noisy-OR / all-empty rule and confidence floor.
- Room sources produce sensor room labels with buffers.
- `LinkedObjective`: one-group parity with current joint fit; shared radar gets one
  value; automatic Apply refused when any group worsens; overnight visits each set
  once.
- Regression: quiet member samples never enter a radar's Present reference.

Browser (`tests/panel.cjs`): shared radar renders in both zones; drafts survive polls
and mirror; collapse per placement; chart selection mirrors; job status updates every
copy; editing members moves cards and keeps open sections; Add zone prefills area;
room timeline labelling and "Seen by" toggles; linked band Learn/Apply; mobile has no
horizontal scroll with nested nodes; no-areas empty state.

Full CI mirror before completion: ruff check/format, complexity, package validation,
pytest with coverage, prettier, panel browser tests.

## 9. Docs

Rewrite README "Rooms and independent zones" and "Joint learning within a zone" for the
room-first model (room labels, attribution, learning sets); update
`docs/architecture.md` for `rooms/layout.py`, `rooms/attribution.py`,
`panel/layout.js` and the new stored keys.
