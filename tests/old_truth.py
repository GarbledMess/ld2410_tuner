"""Reference: room truth as it was before it was made fast.

Each level reads the members' labels again. The efficiency tests check the fast path against this
on random contexts; nothing else uses it."""

import test_tuner  # noqa: F401  (registers tuner_under_test)
from tuner_under_test.calibration.constants import AGREEING_MEMBERS
from tuner_under_test.rooms import truth

LABELS = ("present", "not_present")


def device_label(evidence, timestamp):
    manual = evidence["manual"](timestamp)
    if manual in LABELS:
        return manual, "human", 1.0
    if manual is not None:
        return None
    return evidence["automatic"].at(timestamp)


def room_label(evidence, timestamp):
    labels = [device_label(item, timestamp) for item in evidence.values()]
    present = [item for item in labels if item and item[0] == "present"]
    if present:
        best = max(present, key=lambda item: (item[1] == "human", item[2]))
        return (
            best
            if best[1] == "human" or len(present) >= min(AGREEING_MEMBERS, len(labels))
            else None
        )
    if any(item is None for item in labels):
        return None
    source = "human" if all(item[1] == "human" for item in labels) else "automatic"
    return "not_present", source, min(item[2] for item in labels)


def member_unknown(members, timestamp):
    return any(item["manual"](timestamp) not in (None, *LABELS) for item in members.values())


def room_truth(context, timestamp):
    own = context["own"](timestamp)
    if own is not None:
        return own if own[0] in LABELS else None
    implied = truth._implied(context, timestamp)
    if implied:
        return implied
    derived = room_label(context["members"], timestamp)
    if (derived and derived[1] == "human") or member_unknown(context["members"], timestamp):
        return derived
    return truth._sensor(context, timestamp) or derived or truth._estimate(context, timestamp)
