"""One hybrid facade for every fitting and fixed-pattern scoring surface.

The seven entry points keep the public signatures of the device learner
(``calibration.event_fitting``), the comparison job and the room fitting and
assessment modules. Fits run the hybrid engine; reported measurements always
come from one fixed-pattern evaluation of the final thresholds, so report
scores, adaptive held-out scores, comparisons and Apply share one cost.
Fixed-pattern surfaces (assess, compare, measure, compare_room) never refit.
"""

from dataclasses import replace
from math import inf

from ...calibration.comparison import SCORE_MODEL, best_slots, complete_thresholds
from ...calibration.constants import MIN_RECALL
from ...calibration.episodes import chronological_split
from ...calibration.quality import QUALITY_VERSION
from ...calibration.scoring import episode_share, score_from_cost
from ...calibration.settings import DEFAULT_AUTOMATIC_WEIGHT
from ...calibration.settings import KEY as WEIGHT_KEY
from ...const import HISTORY_KEYS
from ...history import spans
from ...rooms import adaptive, attribution, roles
from . import adapters, engine, fixed, held, reports, room_bundles
from .diagnostics import diagnostics, diagnostics_pair
from .scoring import masked
from .types import FitResult

DEFAULT_THRESHOLD = 50  # A gate's starting point when its current threshold is unknown.
NO_CLASS = "No observed labelled time in one class."
INFLUENCE = 6
STATES = fixed.STATES
SECONDS = {"present": "occupied_seconds", "not_present": "empty_seconds"}


def _named(pattern):
    return {rid: {HISTORY_KEYS[c]: v for c, v in values.items()} for rid, values in pattern.items()}


def _pattern(problem, named):
    return {
        rid: {c: int(named[rid][HISTORY_KEYS[c]]) for c in radar.channels}
        for rid, radar in problem.radars.items()
    }


def _current(problem):
    return {rid: dict(radar.current) for rid, radar in problem.radars.items()}


def _source_zones(zone):
    return {"all": zone, **{name: fixed.source(zone, name) for name in ("human", "automatic")}}


def _by_name(found, zones):
    return {name: found[zone.group_id] for name, zone in zones.items()}


# Device learner -----------------------------------------------------------------------


def _share(policy, state, human, automatic):
    """The automatic influence the common cost gives one class (audit only)."""
    key = SECONDS[state]
    if not human[key]:
        return 1.0 if automatic[key] else 0.0
    weight = 0 if policy.reference else policy.automatic_weight
    support = policy.quality
    stats = (automatic["sufficient_statistics"].get(f"{state}/automatic") or [0, 0, 0, 0])[2]
    found = support.get("automatic_episode_mass", {}).get(state, stats)
    humans = support.get("human_episodes", {}).get(state, 0)
    return episode_share(weight, humans, found)


def _confidence(automatic, state):
    stats = automatic["sufficient_statistics"].get(f"{state}/automatic")
    seconds = automatic[SECONDS[state]]
    return stats[0] / seconds if stats and seconds else None


def _automatic_evidence(problem, measured):
    human, automatic, policy = measured["human"], measured["automatic"], problem.policy
    offered = {s: automatic[SECONDS[s]] / 6 for s in STATES}
    effective = {s: 0 if policy.reference and human[SECONDS[s]] else offered[s] for s in STATES}
    return {
        "offered_samples": offered,
        "samples": effective,
        "used": any(effective.values()),
        "base_weight": policy.automatic_weight,
        "mean_confidence": {s: _confidence(automatic, s) if effective[s] else None for s in STATES},
        "share": {s: _share(policy, s, human, automatic) for s in STATES},
        "priority": "human_first",
        "deferred_samples": 0,
    }


def _bounds(view, device_id, start, end):
    return spans.bounds(view, device_id, start=start, end=end)


def _policy_audit(model):
    return {
        "selected": "corrected_positive",
        "validated": False,
        "reason": "no_valid_chronological_cut",
        "cutoff": None,
        "candidates": {},
        "quality_version": QUALITY_VERSION,
        "provenance": "recorded_basis_only",
        "model": model.audit(),
    }


def _device_policy(build, problem, weight):
    """Training-only candidate fits, held-out selection, as the device learner did."""
    device_id = next(iter(problem.radars))
    model = problem.policy.quality["models"][device_id]
    audit = _policy_audit(model)
    cutoff = chronological_split(model.context.episodes)
    if cutoff is None:
        return audit
    training = build(problem.start, cutoff)
    candidates = held.training_fits(training, weight)

    def named(pattern):
        return _named(pattern)[device_id]

    reports_, empties = held.score_candidates(problem, cutoff, candidates, named)
    choice = held.decide(reports_, empties)
    audit.update(
        selected=choice.policy,
        validated=choice.validated,
        reason=choice.reason,
        cutoff=cutoff,
        candidates=reports_,
        training_model=training.policy.quality["models"][device_id].audit(),
    )
    return audit


def _influence_zones(human, review):
    periods = [row for row in review["periods"] if row["errors"] > 0][:INFLUENCE]
    zones = [
        replace(
            masked(human, [(row["start"], row["end"])]),
            group_id=fixed.tag(human.group_id, "without", str(i)),
        )
        for i, row in enumerate(periods)
    ]
    return periods, zones


def _influence(periods, zones, found, human):
    raw = human["raw"]
    totals = {
        "present": raw["occupied_seconds"] * (1 - (raw["occupied_recall"] or 0)),
        "not_present": raw["false_active_seconds"],
    }
    rows = []
    for row, zone in zip(periods, zones, strict=True):
        error = row["errors"] * 6
        total = totals[row["state"]]
        rows.append(
            {
                "start": row["start"],
                "end": row["end"],
                "state": row["state"],
                "error_seconds": error,
                "error_share_percent": min(100.0, 100 * error / total) if total else 0.0,
                "score_without_period": found[zone.group_id]["score"],
                "comparison": "same_thresholds_not_refitted",
                "excluded_automatically": False,
            }
        )
    return rows


def _validation(scores, problem, human, audit):
    if audit["cutoff"] is None:
        return {}
    later = fixed.since(human, "validation", dict.fromkeys(STATES, audit["cutoff"]))
    device_id = next(iter(problem.radars))
    earlier = audit["candidates"][audit["selected"]]["thresholds"]
    patterns = [_pattern(problem, {device_id: earlier}), _current(problem)]
    found = scores.scores(patterns, [later])
    return {
        "validation": found[0][later.group_id],
        "current_validation": found[1][later.group_id],
    }


def _device_bundle(scores, problem, pattern, audit, review=None):
    zones = _source_zones(problem.zones[0])
    human = zones["human"]
    device_id = next(iter(problem.radars))
    if review is None:
        review = diagnostics(replace(problem, zones=(human,)), pattern, scores.plan)
        review = review["radars"][device_id]
    zones["recent"] = fixed.since(human, "recent", review["recent_cutoffs"])
    periods, without = _influence_zones(human, review["review"])
    found = scores.scores([pattern], [*zones.values(), *without])[0]
    measured = _by_name(found, zones)
    measured["raw"] = scores.scores([pattern], [human], raw=True)[0][human.group_id]
    bundle = {name: measured[name] for name in ("human", "automatic", "recent", "raw")}
    bundle["automatic_evidence"] = _automatic_evidence(problem, measured)
    if problem.policy.reference:
        suppressed = {s for s in STATES if measured["human"][SECONDS[s]]}
        effective = fixed.variant(
            zones["automatic"], "effective", lambda _a, _b, span: span.state not in suppressed
        )
        bundle["automatic"] = scores.scores([pattern], [effective])[0][effective.group_id]
    bundle["influence"] = _influence(periods, without, found, measured["human"])
    bundle.update(_validation(scores, problem, human, audit))
    return bundle, measured["all"]


def _judged(problem, result):
    """``problem`` as the engine finally judged it: discarded anomaly time unobserved."""
    found = (result.diagnostics.get("anomaly") or {}).get("discarded_intervals") or {}
    if not found:
        return problem
    zones = tuple(
        masked(zone, [tuple(span) for span in found.get(zone.group_id, ())])
        for zone in problem.zones
    )
    return replace(problem, zones=zones)


def _device_report(problem, result, audit):
    problem = _judged(engine.with_horizon(problem), result)
    pattern = result.thresholds
    with fixed.FixedScores(problem, 3) as scores:
        collected, review = diagnostics_pair(problem, pattern, scores.plan)
        bundle, after = _device_bundle(scores, problem, pattern, audit, review)
        before = scores.scores([_current(problem)], [problem.zones[0]])[0]
        values = {
            "scoring_mode": scores.mode,
            "score_plan": scores.plan,
            "measurements": bundle,
            "raw_diagnostics": collected,
            "quiet_cost": result.diagnostics["quiet_cost"],
            "selection_cost": result.diagnostics["selection_cost"],
        }
        if "anomaly" in result.diagnostics:
            values["anomaly"] = result.diagnostics["anomaly"]
        fitted = FitResult(pattern, before[problem.zones[0].group_id], after, values)
        return reports.device_report(problem, fitted, audit)


def fit(
    view,
    device_id,
    keys,
    current,
    timing=None,
    automatic_weight=DEFAULT_AUTOMATIC_WEIGHT,
    *,
    constraints=None,
    start=-inf,
    end=inf,
):
    """Learn one radar's thresholds; the evidence policy is chosen on later episodes."""
    start, end = _bounds(view, device_id, start, end)
    current = {key: (current or {}).get(key, DEFAULT_THRESHOLD) for key in keys}

    def build(low, high):
        return adapters.device_problem(
            view, device_id, keys, current, timing or {}, automatic_weight, constraints, low, high
        )

    problem = build(start, end)
    audit = _device_policy(build, problem, automatic_weight)
    final = held.with_reference(problem, held.reference_flag(audit["selected"], automatic_weight))
    return _device_report(final, engine.fit(final), audit)


def _measured(view, device_id, keys, thresholds, timing, weight, start, end, context=None):
    """Horizon-clipped problem and its whole/human/automatic scores of each pattern."""
    problem = adapters.device_problem(
        view,
        device_id,
        keys,
        dict(thresholds),
        timing or {},
        weight,
        None,
        start,
        end,
        quality_context=context,
    )
    attribution.release([view])
    return engine.with_horizon(problem)


def _source_scores(scores, problem, patterns):
    zones = _source_zones(problem.zones[0])
    return [_by_name(found, zones) for found in scores.scores(patterns, zones.values())]


def _error_fraction(metrics, state, source):
    """Confidence-weighted class error already accumulated by the evaluator."""
    total, error, *_ = metrics["sufficient_statistics"].get(f"{state}/{source}", [0, 0])
    return error / total if total else 0.0


def _rank(automatic):
    missed = _error_fraction(automatic, "present", "automatic")
    false = _error_fraction(automatic, "not_present", "automatic")
    return automatic["partial_cost"], missed, false


def assess(view, device_id, thresholds, timing=None, *, quality_context=None, start=-inf, end=inf):
    """One fixed pattern on the radar's labelled history (never refitted)."""
    start, end = _bounds(view, device_id, start, end)
    problem = _measured(
        view,
        device_id,
        list(thresholds),
        thresholds,
        timing,
        DEFAULT_AUTOMATIC_WEIGHT,
        start,
        end,
        quality_context,
    )
    with fixed.FixedScores(problem, 1) as scores:
        measured = _source_scores(scores, problem, [_current(problem)])[0]
    model = problem.policy.quality["models"][device_id]
    return {
        "human": reports.measurement(measured["human"], scores.mode),
        "automatic": reports.measurement(measured["automatic"], scores.mode),
        "automatic_rank": _rank(measured["automatic"]),
        "error_cost": measured["all"]["cost"],
        "scoring_mode": scores.mode,
        "quality": model.audit() if quality_context is not False else None,
        "automatic_evidence": _automatic_evidence(problem, measured),
    }


def _valid(pattern, keys):
    values = pattern["thresholds"]
    return complete_thresholds(values, keys) and all(int(v) == v for v in values.values())


def _sources(measured):
    return {
        s: next((src for src in ("human", "automatic") if measured[src][SECONDS[s]]), None)
        for s in STATES
    }


def _scored(measured, mode):
    whole = measured["all"]
    sources = _sources(measured)
    result = {
        "human": reports.measurement(measured["human"], mode),
        "automatic": reports.measurement(measured["automatic"], mode),
        "sources": sources,
        "basis": "human" if set(sources.values()) == {"human"} else "estimated",
        "scoring_mode": mode,
    }
    if None in sources.values() or whole["cost"] is None:
        return {**result, "reason": NO_CLASS}
    recall = 1 - _error_fraction(whole, "present", sources["present"])
    return {
        **result,
        "score": score_from_cost(whole["cost"]),
        "error_cost": whole["cost"],
        "automatic_error_cost": measured["automatic"]["partial_cost"],
        "ranking": [
            whole["cost"],
            whole["mostly_missed_episodes"],
            whole["completely_missed_episodes"],
            fixed.missed_seconds(whole),
            whole["false_active_seconds"],
            *_rank(measured["automatic"]),
        ],
        "presence_recall": recall,
        "false_positive_percent": 100
        * _error_fraction(whole, "not_present", sources["not_present"]),
        "target_met": recall >= MIN_RECALL - 1e-12,
    }


def _compared(pattern, keys, measured, mode):
    if pattern is None:
        return {"score": None, "reason": "No saved result", "applicable": False}
    valid = _valid(pattern, keys)
    result = {
        "result_id": pattern.get("id"),
        "applicable": pattern["applicable"] and valid,
        "thresholds_complete": valid,
        "score": None,
    }
    if not valid:
        reason = "A complete integer threshold set for the active gates is unavailable"
        return {**result, "reason": reason}
    return {**result, **_scored(measured, mode)}


def _counts(measured):
    return {
        src: {s: measured[src][SECONDS[s]] / 6 for s in STATES} for src in ("human", "automatic")
    }


def compare(view, device_id, context):
    """Saved and live patterns scored together as one fidelity bundle (no learning)."""
    keys = list(context["keys"])
    start, end = _bounds(view, device_id, -inf, inf)
    weight = context.get(WEIGHT_KEY, DEFAULT_AUTOMATIC_WEIGHT)
    # Without a quality version the comparison uses recorded confidence, as before.
    corrected = None if context.get("quality_version") else False
    problem = _measured(
        view,
        device_id,
        keys,
        dict.fromkeys(keys, 100),
        context["timing"],
        weight,
        start,
        end,
        corrected,
    )
    valid = {
        name: pattern
        for name, pattern in context["patterns"].items()
        if pattern is not None and _valid(pattern, keys)
    }
    patterns = [_pattern(problem, {device_id: p["thresholds"]}) for p in valid.values()]
    count = len({tuple(sorted(p["thresholds"].items())) for p in valid.values()})
    count = max(count, context.get("fidelity_pattern_count", count))
    with fixed.FixedScores(problem, count) as scores:
        measured = _source_scores(scores, problem, patterns or [_current(problem)])
    found = dict(zip(valid, measured, strict=False))
    compared = {
        name: _compared(pattern, keys, found.get(name), scores.mode)
        for name, pattern in context["patterns"].items()
    }
    result = {
        "model": SCORE_MODEL,
        "patterns": compared,
        "best_slots": best_slots(compared),
        "counts": _counts(measured[0]),
        "timing": context["timing"],
        "incomplete_samples": measured[0]["all"]["uncovered_label_seconds"] / 6,
        "outliers": {"method": "native_intervals_retained", "excluded": False},
        "missed_time_cost": 5,
        "scoring_mode": scores.mode,
    }
    if context.get("quality_version"):
        result["quality"] = problem.policy.quality["models"][device_id].audit()
    return result


# Rooms --------------------------------------------------------------------------------


def _seen(groups, rooms):
    return {gid: roles.attributed_yes(found, rooms.get(gid)) for gid, found in groups.items()}


def _room_problem(groups, start, end, rooms, weight, contexts):
    contexts = None if contexts is None else adaptive.models(contexts)
    return adapters.room_problem(groups, start, end, rooms, weight, quality_contexts=contexts)


def _zone_problem(problem, zone):
    radars = {k: v for k, v in problem.radars.items() if k in zone.members}
    return replace(problem, radars=radars, zones=(zone,))


def _assessments(problem, zone, patterns, seen):
    with fixed.FixedScores(problem, len(patterns)) as scores:
        found = room_bundles.bundles(scores, zone, patterns, seen)
        own = _zone_problem(problem, zone)
        return [reports._room_view(own, whole, bundle, scores.mode) for bundle, whole in found]


def _single(members, start, end, room, quality):
    rooms = {"room": room} if room else {}
    groups = {"room": members}
    problem = engine.with_horizon(
        _room_problem(groups, start, end, rooms, DEFAULT_AUTOMATIC_WEIGHT, quality)
    )
    return problem, _seen(groups, rooms)["room"]


def measure(members, thresholds, start, end, room=None, *, quality=None):
    """One fixed pattern assessed on ``room`` truth (None: member labels only)."""
    candidates = {key: {**item, "thresholds": thresholds[key]} for key, item in members.items()}
    problem, seen = _single(candidates, start, end, room, quality)
    return _assessments(problem, problem.zones[0], [_current(problem)], seen)[0]


def compare_room(members, thresholds, start, end, room=None):
    """Live and candidate room results for one zone on the same recordings."""
    problem, seen = _single(members, start, end, room, None)
    patterns = [_current(problem), _pattern(problem, thresholds)]
    live, candidate = _assessments(problem, problem.zones[0], patterns, seen)
    return {"live": live["room"], "candidate": candidate["room"]}


def _subset(problem, gids):
    zones = tuple(z for z in problem.zones if z.group_id in gids)
    rids = {m for z in zones for m in z.members}
    quality = dict(problem.policy.quality)
    for field in ("models", "audits"):
        quality[field] = {
            key: value for key, value in quality.get(field, {}).items() if key in rids
        }
    quality["zones"] = {
        key: value for key, value in quality.get("zones", {}).items() if key in gids
    }
    if len(zones) == 1:
        quality.update(quality.get("zones", {}).get(zones[0].group_id, {}))
    return replace(
        problem,
        zones=zones,
        radars={k: v for k, v in problem.radars.items() if k in rids},
        rows={k: v for k, v in problem.rows.items() if k in rids},
        policy=replace(problem.policy, quality=quality),
    )


def _room_audit(models):
    return {
        "quality": {key: model.audit() for key, model in models.items()},
        "selection": {
            "policy": "corrected_positive",
            "validated": False,
            "reason": "no_valid_chronological_cut",
            "cutoff": None,
            "candidates": {},
            "quality_version": QUALITY_VERSION,
            "provenance": "recorded_basis_only",
        },
        "sensor_provenance": "recorded_owner_unavailable_prior_only",
        "derived_estimate_episode_mass": 0,
    }


def _truth_sources(zone):
    found = {(s.state, s.source == "human") for s in zone.truth}
    return {
        s: "human" if (s, True) in found else "automatic" if (s, False) in found else None
        for s in STATES
    }


def _room_policy(problem, groups, rooms, weight):
    """Two linked training fits before a whole-episode cut, chosen on later episodes."""
    models = problem.policy.quality["models"]
    audit = _room_audit(models)
    independent = [ep for model in models.values() for ep in model.context.episodes]
    zoned = [ep for zone in problem.zones for ep in fixed.episodes(zone, zone.group_id)]
    cutoff = chronological_split({*independent, *zoned})
    if cutoff is None:
        return "corrected_positive", audit
    earlier, clipped = adaptive._earlier(groups, rooms, cutoff)
    training = adapters.room_problem(earlier, problem.start, cutoff, clipped, weight)
    candidates = held.training_fits(training, weight)
    reports_, empties = held.score_candidates(problem, cutoff, candidates, _named)
    choice = held.decide(reports_, empties)
    audit["selection"].update(
        policy=choice.policy,
        validated=choice.validated,
        reason=choice.reason,
        cutoff=cutoff,
        candidates=reports_,
        reference_sources={zone.group_id: _truth_sources(zone) for zone in training.zones},
        training_quality={
            key: model.audit() for key, model in training.policy.quality["models"].items()
        },
    )
    return choice.policy, audit


def _top_level(groups, reports_, which):
    """Several zones: no union room truth, only each radar's measure per zone."""
    radars = dict.fromkeys(m for found in groups.values() for m in found)
    return {
        "room": None,
        "devices": {
            radar: {
                gid: item[which]["devices"][radar]
                for gid, item in reports_.items()
                if "after" in item and radar in item[which]["devices"]
            }
            for radar in radars
        },
    }


class _Room:
    """One linked set: per-zone bundles before and after one shared fit."""

    def __init__(self, groups, rooms, problem):
        self.groups, self.rooms = groups, rooms
        self.problem = engine.with_horizon(problem)
        self.seen = _seen(groups, rooms)

    def bundles(self, scores, zones, pattern):
        return {
            z.group_id: room_bundles.bundles(scores, z, [pattern], self.seen.get(z.group_id))[0]
            for z in zones
        }

    def skipped(self, scores):
        before = scores.scores([_current(self.problem)], self.problem.zones)[0]
        gids = sorted(before)
        skipped = {gid: NO_CLASS for gid in gids if before[gid]["cost"] is None}
        if len(skipped) == len(gids):
            raise ValueError(skipped[gids[0]])
        return skipped


def _metrics(problem, found):
    """Flat metrics for a one-zone set (as ``room_report`` reads them), else per zone."""
    if len(problem.zones) == 1:
        return next(iter(found.values()))[1]
    zones = {gid: whole for gid, (_, whole) in found.items()}
    return {"zones": zones, "cost": sum(z["cost"] for z in zones.values())}


def _fitted(room, scores, result, skipped, before):
    problem = room.problem
    thresholds = {**_current(problem), **result.thresholds}
    zones = [z for z in problem.zones if z.group_id not in skipped]
    after = room.bundles(scores, zones, thresholds)
    for gid, (bundle, _) in after.items():
        bundle["before"] = before[gid][0]
    kept = {gid: before[gid] for gid in after}
    diagnostics_ = {
        "scoring_mode": scores.mode,
        "score_plan": scores.plan,
        "zones": {gid: bundle for gid, (bundle, _) in after.items()},
        "quiet_cost": result.diagnostics["quiet_cost"],
        "selection_cost": result.diagnostics["selection_cost"],
    }
    return FitResult(thresholds, _metrics(problem, kept), _metrics(problem, after), diagnostics_)


def fit_set(
    groups,
    start,
    end,
    rooms=None,
    automatic_weight=DEFAULT_AUTOMATIC_WEIGHT,
    *,
    quality_contexts=None,
):
    """Fit a linked set; zones without observed time in both classes are skipped.

    A radar serving only skipped zones keeps its current thresholds and is not
    searched. One zone reports exactly as ``room_fit``; several report each zone
    in ``groups`` and leave top-level ``room`` None (no artificial union truth).
    """
    if not groups:
        raise ValueError("No zones to learn")
    rooms = rooms or {}
    problem = _room_problem(groups, start, end, rooms, automatic_weight, quality_contexts)
    room = _Room(groups, rooms, problem)
    with fixed.FixedScores(room.problem, 1) as scores:
        skipped = room.skipped(scores)
    scorable = _subset(problem, set(room.seen) - set(skipped))
    audit = None
    policy = "corrected_positive"
    if quality_contexts is not None:
        kept = {gid: groups[gid] for gid in groups if gid not in skipped}
        policy, audit = _room_policy(scorable, kept, rooms, automatic_weight)
    reference = held.reference_flag(policy, automatic_weight)
    result = engine.fit(held.with_reference(scorable, reference))
    # Judge before and after with the selected evidence policy and the fit's masks.
    room.problem = _judged(held.with_reference(room.problem, reference), result)
    with fixed.FixedScores(room.problem, 2) as scores:
        before = room.bundles(scores, room.problem.zones, _current(room.problem))
        fitted = _fitted(room, scores, result, skipped, before)
    if audit is not None:
        fitted.diagnostics["automatic_evidence"] = audit
    report = reports.room_report(room.problem, fitted, skipped, result.diagnostics.get("anomaly"))
    if len(groups) > 1:
        for which in ("before", "after"):
            report[which] = _top_level(groups, report["groups"], which)
    return report


def room_fit(
    members,
    start,
    end,
    room=None,
    automatic_weight=DEFAULT_AUTOMATIC_WEIGHT,
    *,
    quality_contexts=None,
):
    """One zone's fit: exactly ``fit_set`` of that zone, without ``groups``."""
    result = fit_set(
        {"room": members},
        start,
        end,
        {"room": room} if room else {},
        automatic_weight,
        quality_contexts=quality_contexts,
    )
    result.pop("groups")
    return result
