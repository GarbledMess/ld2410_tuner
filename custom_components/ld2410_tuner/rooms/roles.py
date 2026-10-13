"""Each member's role in its room: attributed-yes time and per-radar occupied/empty tallies."""

from __future__ import annotations

from math import nextafter

from ..calibration.constants import MIN_AUTO_CONFIDENCE
from . import attribution
from .intervals import Timeline

ROOM = "room"  # Group id for a room-less assessment: no labels, only member labels.
FIELDS = (
    "missed_seen_seconds",
    "missed_seen_inferred_seconds",
    "missed_seen_automatic_seconds",
    "false_active_seconds",
    "occupied_seconds",
    "empty_seconds",
)


def _inputs(members, room, key):
    """``(saved, group, others)`` to attribute member ``key`` as the learner does: its view's
    room snapshot (every group it is in, so one baseline under the union rule) and members'
    recorded guesses over full history (``others`` None reads them through the view). A
    radar in no multi-radar group has no snapshot: this group alone stands in, with each
    member's guesses read from its own view."""
    room = room or {}
    group = room.get("group_id") or ROOM
    saved = members[key]["view"].data.get("room_snapshot")
    if saved and group in saved["own"]:
        return saved, group, None
    groups = room.get("groups", {})
    devices = {name: item["view"].data["devices"][name] for name, item in members.items()}
    others = {name: attribution.member_guess(members[name]["view"], name) for name in devices}
    saved = {
        "device_id": key,
        "own": [group],
        "groups": {**groups, group: {**groups.get(group, {}), "device_ids": list(members)}},
        "room_labels": room.get("room_labels", {}),
        "members": {name: attribution.label_fields(device) for name, device in devices.items()},
    }
    return saved, group, others


def _learned_yes(result):
    """The learner's "yes": a human label (confidence 1) or a rule-2 guess at or above the
    floor, so one comparison covers both; automatic spans use the same floor."""
    return result["state"] == "yes" and result["confidence"] >= MIN_AUTO_CONFIDENCE


def _timeline(learned, group):
    return Timeline(
        [
            (span[0], nextafter(span[1], float("inf")), bool(result.get("inferred")))
            for span, result in learned["found"][group]
            if _learned_yes(result)
        ]
    )


def attributed_yes(members, room) -> dict[str, dict[str, Timeline]]:
    """Per member, the episodes the learner treats as attributed "yes", for spans of human
    room truth (``human``: human truth and baseline, value = inferred by rule 2) and of
    automatic room truth (``automatic``: sensor labels and the estimate from members'
    recorded guesses). Both use ``radar_attribution``, the function behind the learner's
    ``room_targets`` / ``auto_targets`` and the panel's Seen by."""
    found = {}
    for key, item in members.items():
        saved, group, others = _inputs(members, room, key)
        view = item["view"]
        found[key] = {
            "human": _timeline(
                attribution.radar_attribution(view, key, saved, others=others), group
            ),
            "automatic": _timeline(
                attribution.radar_attribution(view, key, saved, human=False, others=others),
                group,
            ),
        }
    return found


def target(members, room, key):
    """The learner's target for member ``key`` in this group (``attribution.group_target``)."""
    saved, group, others = _inputs(members, room, key)
    return attribution.group_target(members[key]["view"], key, saved, group, others)


def attributed(seen, label, t):
    """``(attributed, automatic)`` for one member's ``seen`` timelines at ``t``: the
    human-truth attribution where room truth ``label`` is human, else the automatic one."""
    automatic = not attribution.is_human(label)
    return seen["automatic" if automatic else "human"].at(t), automatic


def empty_roles():
    return dict.fromkeys(FIELDS, 0.0)


def add(tally, duration, state, hit, attributed, automatic):
    """Tally one measured span for one member. ``attributed`` is None (not attributed "yes")
    or whether that "yes" is inferred (rule 2); ``automatic`` marks automatic room truth."""
    if state == "present":
        tally["occupied_seconds"] += duration
        if attributed is not None and not hit:
            tally["missed_seen_seconds"] += duration
            if automatic:
                tally["missed_seen_automatic_seconds"] += duration
            else:
                tally["missed_seen_inferred_seconds"] += duration * attributed
    else:
        tally["empty_seconds"] += duration
        tally["false_active_seconds"] += duration * hit
