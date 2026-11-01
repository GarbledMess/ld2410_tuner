"""Room assessment aggregates for fixed patterns, all from one evaluator.

A zone bundle carries the zone's whole, human, automatic and recent truth
scores, the untimed human score, and per member its own score against the zone
truth, by source, plus its missed time inside Seen-by attributed presence.
"""

from . import fixed
from .types import ZoneSpec

SOURCES = ("human", "automatic")


def _spans(timeline, inferred=False):
    if timeline is None:
        return []
    return [(span[0], span[1]) for span in timeline.spans if not inferred or span[2]]


def _member_zones(zone, member, seen):
    alone = ZoneSpec(fixed.tag(zone.group_id, "member", member), (member,), zone.truth)
    human, automatic = (fixed.source(alone, name) for name in SOURCES)
    seen = seen or {}
    return {
        "metrics": alone,
        "human": human,
        "automatic": automatic,
        "seen_human": fixed.within(human, "seen", _spans(seen.get("human")), (member,)),
        "seen_inferred": fixed.within(
            human, "inferred", _spans(seen.get("human"), True), (member,)
        ),
        "seen_automatic": fixed.within(automatic, "seen", _spans(seen.get("automatic")), (member,)),
    }


def variants(zone, seen):
    """``(zone variants, member variants)`` for one zone and its Seen-by timelines."""
    human, automatic = (fixed.source(zone, name) for name in SOURCES)
    base = {
        "all": zone,
        "human": human,
        "automatic": automatic,
        "recent": fixed.since(human, "recent", fixed.label_cutoffs(human)),
    }
    members = {m: _member_zones(zone, m, (seen or {}).get(m)) for m in zone.members}
    return base, members


def _member(found, zones, exclusive):
    missed = {name: fixed.missed_seconds(found[z.group_id]) for name, z in zones.items()}
    return {
        "metrics": found[zones["metrics"].group_id],
        "sources": {name: found[zones[name].group_id] for name in SOURCES},
        "exclusive_presence_seconds": exclusive,
        "missed_seen_seconds": missed["seen_human"] + missed["seen_automatic"],
        "missed_seen_inferred_seconds": missed["seen_inferred"],
        "missed_seen_automatic_seconds": missed["seen_automatic"],
    }


def bundles(scores, zone, patterns, seen):
    """Per pattern ``(bundle, zone metrics)`` scored together at one fidelity."""
    base, members = variants(zone, seen)
    every = [*base.values(), *(z for found in members.values() for z in found.values())]
    timed = scores.scores(patterns, every)
    raw = scores.scores(patterns, [base["human"]], raw=True)
    result = []
    for found, untimed in zip(timed, raw, strict=True):
        whole = found[zone.group_id]
        bundle = {name: found[z.group_id] for name, z in base.items() if name != "all"}
        bundle["raw"] = untimed[base["human"].group_id]
        bundle["members"] = {
            m: _member(found, zones, whole["exclusive_seconds"].get(m, 0.0))
            for m, zones in members.items()
        }
        result.append((bundle, whole))
    return result
