"""Compare fixed gate patterns on common evidence without learning or writing gates."""

import math

from .constants import MIN_CLASS_SAMPLES, MIN_RECALL, MISSED_TIME_COST
from .duration import DurationReplay, _error_cost
from .evidence import collect_samples
from .reliability import prepare_evidence
from .timing_metrics import _hit_mask, evaluate

SCORE_MODEL = "weighted_time_v1"


def score_from_cost(cost):
    """100 requires zero measured error; rounding must not turn small errors into perfection."""
    if not math.isfinite(cost) or cost < 0:
        raise ValueError("Score requires a finite, non-negative error cost")
    if cost == 0:
        return 100.0
    return min(99.99, round(max(0.0, 100.0 - cost), 2))


def _groups(rows, keys):
    return {
        label: [row for row in rows if row[2] == label and all(key in row[1] for key in keys)]
        for label in ("present", "not_present")
    }


def calculate(view, device_id, context):
    keys, timing = context["keys"], context["timing"]
    rows, guesses = collect_samples(view, device_id, keys)
    human, automatic = _groups(rows, keys), _groups(guesses, keys)
    incomplete = (
        len(rows) + len(guesses) - sum(len(g) for g in (*human.values(), *automatic.values()))
    )
    human, automatic, exclusions, _ = prepare_evidence(human, automatic, keys)
    patterns = {
        name: _measure_pattern(pattern, keys, human, automatic, timing)
        for name, pattern in context["patterns"].items()
    }
    eligible = {
        name: item
        for name, item in patterns.items()
        if name != "live" and item.get("applicable") and item.get("score") is not None
    }
    best = min((_rank(item) for item in eligible.values()), default=None)
    return {
        "model": SCORE_MODEL,
        "patterns": patterns,
        "best_slots": [name for name, item in eligible.items() if _rank(item) == best],
        "timing": timing,
        "counts": {
            "human": {k: len(v) for k, v in human.items()},
            "automatic": {k: len(v) for k, v in automatic.items()},
        },
        "incomplete_samples": incomplete,
        "outliers": exclusions,
        "missed_time_cost": MISSED_TIME_COST,
    }


def _rank(item):
    # Match the learner: automatic evidence only breaks ties in supported human evidence.
    return tuple(item["ranking"])


def _measure_pattern(pattern, keys, human, automatic, timing):
    if pattern is None:
        return {"score": None, "reason": "No saved result", "applicable": False}
    result = {"result_id": pattern.get("id"), "applicable": pattern["applicable"]}
    values = pattern["thresholds"]
    if set(values) != set(keys) or not all(_valid_threshold(value) for value in values.values()):
        return {
            **result,
            "score": None,
            "reason": "A complete threshold set for the active gates is unavailable",
            "applicable": False,
        }
    measured = evaluate(human, values, timing)
    estimated = evaluate(automatic, values, timing)
    replay = DurationReplay(automatic["present"], automatic["not_present"], timing)
    auto_cost, auto_miss, auto_false = replay.automatic_rank(
        _hit_mask(automatic["present"], values), _hit_mask(automatic["not_present"], values)
    )
    sources = {label: _source(label, measured, estimated) for label in human}
    result.update(human=measured, automatic=estimated, sources=sources)
    if None in sources.values():
        return {
            **result,
            "score": None,
            "reason": "Need at least 50 usable observations and measurable time for both occupied and empty states",
        }
    duration = measured["duration"]
    miss = 1 - duration["presence_recall_lower"] if sources["present"] == "human" else auto_miss
    false = (
        duration["false_positive_percent"] / 100
        if sources["not_present"] == "human"
        else auto_false
    )
    cost = _error_cost(miss, false)
    human_replay = DurationReplay(human["present"], human["not_present"], timing)
    human_rank = human_replay.rank(
        _hit_mask(human["present"], values), _hit_mask(human["not_present"], values)
    )
    return {
        **result,
        "score": score_from_cost(cost),
        "error_cost": cost,
        "automatic_error_cost": auto_cost,
        "ranking": [round(cost, 10), *human_rank[1:], auto_cost, auto_miss, auto_false],
        "presence_recall": 1 - miss,
        "false_positive_percent": 100 * false,
        "target_met": 1 - miss >= MIN_RECALL - 1e-12,
        "basis": "human" if set(sources.values()) == {"human"} else "estimated",
    }


def _source(label, human, automatic):
    samples, seconds = (
        ("present_samples", "present_seconds")
        if label == "present"
        else ("not_present_samples", "empty_seconds")
    )
    if human[samples] + automatic[samples] < MIN_CLASS_SAMPLES:
        return None
    for name, measured in (("human", human), ("automatic", automatic)):
        if measured["duration"][seconds] > 0:
            return name
    return None


def _valid_threshold(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 100
