"""Evidence-aware selection, the quiet-presence prior and the fit facade."""

import random
from dataclasses import replace

import pytest
import test_tuner  # noqa: F401
from test_hybrid_evidence import truth
from test_hybrid_exact import Rows
from test_hybrid_search import Days
from tuner_under_test.engine.hybrid.types import EvidencePolicy, FitProblem, RadarSpec, ZoneSpec

DAY = 86400


def episodes(count, seconds=60, present=(60, 70), empty=(4, 9), quiet=False, seed=1):
    """Alternating labelled episodes as six-second rows of one channel."""
    rng = random.Random(seed)
    rows, spans, t = [], [], 0
    for i in range(count):
        state = "present" if i % 2 == 0 else "not_present"
        lo, hi = present if state == "present" else empty
        for k in range(seconds // 6):
            value = rng.randint(lo, hi)
            if quiet and state == "present" and k % 2:
                value = rng.randint(25, 30)  # quiet sitting: still clearly above empty
            rows.append((t + 6 * k, t + 6 * (k + 1), [value] + [255] * 17))
        spans.append(truth(t, t + seconds, state, episode=f"{state}{i}"))
        t += seconds
    return rows, tuple(spans)


def fit_problem(rows, spans, current=90, timing=None, policy=None, rid="a"):
    radar = RadarSpec(rid, (0,), {0: tuple(range(101))}, {0: current}, timing or {})
    return FitProblem(
        {rid: radar},
        (ZoneSpec("z", (rid,), spans),),
        0,
        rows[-1][1],
        Days({rid: rows}),
        {rid: Rows(rows)},
        policy or EvidencePolicy(0, False, {}),
    )


def result(cost, mm=0, active=0, occ=2, emp=4):
    return {
        "cost": cost,
        "partial_cost": cost,
        "mostly_missed_episodes": mm,
        "empty_periods_mostly_false_active": active,
        "occupied_episodes": occ,
        "empty_periods": emp,
    }


def chooser(n, occupied=3600.0):
    spans = (truth(0, occupied), truth(occupied, 2 * occupied, "not_present", episode="e"))
    radar = RadarSpec("R", (0,), {0: tuple(range(101))}, {0: 50}, {})
    problem = FitProblem(
        {"R": radar},
        (ZoneSpec("z", ("R",), spans),),
        0,
        2 * occupied,
        None,
        {},
        EvidencePolicy(0, False, {}),
    )
    stats = {("R", 0): {"gap": None}}
    return problem, stats, [{"R": {0: 50 + i}} for i in range(n)]


def wrap(results, gid="z"):
    return [{**r, "zones": {gid: r}} for r in results]


def test_choose_never_accepts_more_mostly_missed_or_mostly_false_active_periods():
    from tuner_under_test.engine.hybrid.selection import choose

    problem, stats, pats = chooser(4)
    results = [result(100.0), result(10.0, mm=1), result(20.0, active=1), result(60.0)]
    pick, info = choose(problem, pats, wrap(results), None, stats)
    assert pick == 3
    assert info["guarded"] == 2
    results[0] = result(100.0, mm=1, active=1)  # guards are relative to the start
    assert choose(problem, pats, wrap(results), None, stats)[0] == 1


def test_thin_evidence_needs_a_bigger_improvement_to_move():
    from tuner_under_test.engine.hybrid.domains import evidence_margin
    from tuner_under_test.engine.hybrid.selection import choose

    problem, stats, pats = chooser(2)
    thin = [result(100.0, occ=1, emp=1), result(91.0, occ=1, emp=1)]
    assert choose(problem, pats, wrap(thin), None, stats)[0] == 0
    assert evidence_margin(1, 1) == pytest.approx(10.5)
    rich = [result(100.0, occ=20, emp=20), result(91.0, occ=20, emp=20)]
    assert choose(problem, pats, wrap(rich), None, stats)[0] == 1


def test_band_boundary_does_not_move():
    from tuner_under_test.engine.hybrid.selection import choose

    problem, stats, pats = chooser(2)
    exactly = [result(100.0, occ=20, emp=20), result(99.0, occ=20, emp=20)]  # margin 1.0
    pick, info = choose(problem, pats, wrap(exactly), None, stats)
    assert pick == 0
    assert info["why"] == "improvement within evidence margin"
    beyond = [result(100.0, occ=20, emp=20), result(98.999, occ=20, emp=20)]
    assert choose(problem, pats, wrap(beyond), None, stats)[0] == 1


def test_ties_within_margin_prefer_presence_favouring_lower_thresholds():
    from tuner_under_test.engine.hybrid.selection import choose

    problem, stats, pats = chooser(3)
    pats[1] = {"R": {0: 70}}
    pats[2] = {"R": {0: 60}}
    results = [result(100.0), result(50.0), result(52.0)]  # margin 5.5: 52 ties with 50
    assert choose(problem, pats, wrap(results), None, stats)[0] == 2


def test_ties_within_margin_prefer_the_most_central_gap_threshold():
    from tuner_under_test.engine.hybrid.selection import choose

    problem, stats, pats = chooser(4)
    stats[("R", 0)] = {"gap": (10, 60)}
    pats[1:] = [{"R": {0: 15}}, {"R": {0: 35}}, {"R": {0: 55}}]
    results = [result(100.0), result(50.0), result(52.0), result(51.0)]
    assert choose(problem, pats, wrap(results), None, stats)[0] == 2


def test_linked_guards_are_checked_per_zone():
    from tuner_under_test.engine.hybrid.selection import choose

    problem, stats, pats = chooser(2)
    other = replace(problem.zones[0], group_id="y")
    problem = replace(problem, zones=(*problem.zones, other))
    start = {"z": result(100.0, mm=1), "y": result(100.0, mm=0)}
    moved = {"z": result(10.0, mm=0), "y": result(10.0, mm=1)}  # same total, one zone worse
    scores = [
        {"cost": 200.0, "partial_cost": 200.0, "zones": start},
        {"cost": 20.0, "partial_cost": 20.0, "zones": moved},
    ]
    pick, info = choose(problem, pats, scores, None, stats)
    assert pick == 0
    assert info["guarded"] == 1


def test_quiet_cost_has_slack_so_thresholds_do_not_ride_the_empty_edge():
    from tuner_under_test.engine.hybrid.selection import quiet_cost

    assert quiet_cost({"time_term": 5 * 0.05, "episode_term": 0.0}) == 0.0
    assert quiet_cost({"time_term": 5 * 0.30, "episode_term": 0.0}) == pytest.approx(100)
    assert quiet_cost({"time_term": 5 * 0.60, "episode_term": 5 * 0.5}) == pytest.approx(
        500 * 0.5 + 250
    )


def test_quiet_weight_shrinks_with_occupied_evidence():
    from tuner_under_test.engine.hybrid.selection import quiet_weight

    assert quiet_weight(0) == 1.0
    assert quiet_weight(14400) == 1.0
    assert quiet_weight(28800) == pytest.approx(0.5)


def test_selection_cost_caps_the_quiet_prior():
    from tuner_under_test.engine.hybrid.selection import choose

    problem, stats, pats = chooser(2, occupied=3600.0)  # weight 1
    base = wrap([result(100.0, occ=20, emp=20), result(60.0, occ=20, emp=20)])
    quiet = wrap([{"time_term": 0.0, "episode_term": 0.0}, {"time_term": 5.0, "episode_term": 5.0}])
    pick, info = choose(problem, pats, base, quiet, stats)
    assert info["selection_costs"][1] == pytest.approx(60.0 + 100.0)  # 1 x 1000 capped at 100
    assert pick == 0


def _human_stats(recall, automatic=None):
    stats = {"present/human": [100.0, 100.0 * (1 - recall), 1.0, 0.0]}
    if automatic is not None:
        stats["present/automatic"] = [100.0, 100.0 * (1 - automatic), 1.0, 0.0]
    return stats


def _recalled(cost, recall, automatic=None):
    return {
        **result(cost, occ=20, emp=20),
        "sufficient_statistics": _human_stats(recall, automatic),
    }


def test_cheaper_candidate_cannot_give_up_presence_beyond_the_floor():
    from tuner_under_test.engine.hybrid.selection import choose

    problem, stats, pats = chooser(3)
    base = wrap([_recalled(100.0, 0.9), _recalled(40.0, 0.95), _recalled(60.0, 1.0)])
    pick, info = choose(problem, pats, base, None, stats)
    assert pick == 2
    assert info["presence_floored"] == 1


def test_presence_floor_allows_candidates_within_its_slack():
    from tuner_under_test.engine.hybrid.selection import choose

    problem, stats, pats = chooser(3)
    base = wrap([_recalled(100.0, 0.9), _recalled(40.0, 0.996), _recalled(60.0, 1.0)])
    assert choose(problem, pats, base, None, stats)[0] == 1


def test_presence_floor_never_removes_the_start():
    from tuner_under_test.engine.hybrid.selection import choose

    problem, stats, pats = chooser(2)
    base = wrap([_recalled(50.0, 0.5), _recalled(50.2, 1.0)])
    pick, info = choose(problem, pats, base, None, stats)
    assert pick == 0
    assert info["why"] == "improvement within evidence margin"


def test_presence_floor_uses_the_worst_zone_and_tolerates_missing_human_presence():
    from tuner_under_test.engine.hybrid.selection import _recall

    high, low = (_recalled(1.0, r) for r in (1.0, 0.5))
    assert _recall({"zones": {"a": high, "b": low}}) == 0.5
    assert _recall({"zones": {"a": {}, "b": low}}) == 0.5
    assert _recall({}) == 0.0


def test_presence_floor_ignores_automatic_presence():
    from tuner_under_test.engine.hybrid.selection import choose

    problem, stats, pats = chooser(3)
    base = wrap(
        [_recalled(100.0, 0.9), _recalled(40.0, 1.0, automatic=0.0), _recalled(60.0, 1.0, 1.0)]
    )
    assert choose(problem, pats, base, None, stats)[0] == 1


def test_zero_class_time_keeps_current_without_proposals():
    from tuner_under_test.engine.hybrid.engine import fit

    rows, spans = episodes(4)
    only_present = tuple(s for s in spans if s.state == "present")
    got = fit(fit_problem(rows, only_present))
    assert got.thresholds == {"a": {0: 90}}
    assert got.diagnostics["status"] == "insufficient"
    assert got.diagnostics["insufficient_classes"] == ["not_present"]
    assert got.diagnostics["finalists"] == 1
    assert got.before == got.after


def test_fit_moves_to_the_supported_gap_and_reports_costs_separately():
    from tuner_under_test.engine.hybrid.engine import fit
    from tuner_under_test.engine.hybrid.exact import exact_scores, regions

    rows, spans = episodes(20, seconds=120)
    p = fit_problem(rows, spans)
    got = fit(p)
    assert 9 <= got.thresholds["a"][0] < 60
    d = got.diagnostics
    assert d["status"] == "fitted"
    assert d["scoring_mode"] == "exact"
    assert d["selection_cost"] == pytest.approx(
        d["base_cost"] + min(100.0, d["quiet_weight"]["z"] * d["quiet_cost"])
    )
    exact = exact_scores(p, [got.thresholds], regions(spans))[0]
    assert got.after["cost"] == pytest.approx(exact["cost"])
    assert d["base_cost"] == pytest.approx(exact["cost"])
    assert got.before["cost"] > got.after["cost"]


def test_quiet_prior_is_not_heldout_error_cost():
    from tuner_under_test.engine.hybrid.engine import fit
    from tuner_under_test.engine.hybrid.exact import exact_scores, regions

    rows, spans = episodes(6, quiet=True)
    p = fit_problem(rows, spans)
    got = fit(p)
    assert "quiet" not in got.after
    assert "quiet" not in got.before
    assert got.diagnostics["quiet_weight"]["z"] == 1.0
    pure = exact_scores(p, [got.thresholds], regions(spans))[0]
    assert got.after["cost"] == pytest.approx(pure["cost"])
    assert got.diagnostics["selection_cost"] >= got.after["cost"]


def test_quiet_candidate_search_runs_only_for_short_occupied_evidence(monkeypatch):
    from tuner_under_test.engine.hybrid import selection
    from tuner_under_test.engine.hybrid.engine import fit

    rows, spans = episodes(10, quiet=True)  # 300 s occupied: full prior weight
    p = fit_problem(rows, spans)
    assert fit(p).diagnostics["quiet_search"] is True
    monkeypatch.setattr(selection, "QUIET_SECONDS", 15.0)  # same data now counts as long
    assert fit(p).diagnostics["quiet_search"] is False


def test_fit_is_deterministic_and_independent_of_radar_names():
    from tuner_under_test.engine.hybrid.engine import fit

    rows, spans = episodes(12, seed=4)
    first = fit(fit_problem(rows, spans))
    again = fit(fit_problem(rows, spans))
    renamed = fit(fit_problem(rows, spans, rid="zz"))
    assert first == again
    assert renamed.thresholds == {"zz": first.thresholds["a"]}


def test_fit_reads_only_days_with_labels():
    from tuner_under_test.engine.hybrid.engine import fit

    rows, spans = episodes(8)

    class LabelDays(Days):
        def __init__(self, rows):
            super().__init__(rows)
            self.read = []

        def dates(self, rid):
            return iter((0, 1, 2))

        def bounds(self, date):
            return date * DAY, (date + 1) * DAY

        def digest(self, rid, date, channels):
            self.read.append(date)
            return super().digest(rid, date, channels) if date == 0 else None

    days = LabelDays({"a": rows})
    fit(replace(fit_problem(rows, spans), days=days, end=3 * DAY))
    assert set(days.read) == {0}


def test_horizon_clips_only_automatic_and_keeps_newest_human_labels():
    from tuner_under_test.engine.hybrid.evidence import evidence_horizon

    policy = EvidencePolicy(0, False, {}, automatic_horizon_days=28, human_evidence_hours=2)
    spans = (
        truth(0, 10 * DAY, "not_present", "entity", 0.8, episode="old-auto"),
        truth(0, 1800, episode="h1"),
        truth(4000, 7600, "not_present", episode="h2"),
        truth(30 * DAY, 30 * DAY + 3600, episode="h3"),
        truth(5 * DAY, 50 * DAY, "present", "entity", 0.7, episode="auto"),
        truth(0, 100, "unknown", "entity", episode="u"),
    )
    got = evidence_horizon(spans, 50 * DAY, policy)
    # Automatic spans keep only the last 28 days; crossing intervals are clipped.
    assert [s.episode_id for s in got if s.source != "human"] == ["auto"]
    assert next(s for s in got if s.episode_id == "auto").start == 22 * DAY
    # Newest human labels up to two hours; the label crossing the cap is kept whole.
    assert [s.episode_id for s in got if s.source == "human"] == ["h2", "h3"]
    unlimited = EvidencePolicy(0, False, {}, automatic_horizon_days=None, human_evidence_hours=None)
    assert evidence_horizon(spans, 50 * DAY, unlimited) == spans


def test_policy_defaults_are_the_settled_horizons():
    from tuner_under_test.engine.hybrid.constants import (
        AUTOMATIC_HORIZON_DAYS,
        HUMAN_EVIDENCE_HOURS,
    )

    policy = EvidencePolicy(0, False, {})
    assert (AUTOMATIC_HORIZON_DAYS, HUMAN_EVIDENCE_HOURS) == (28, 400)
    assert policy.automatic_horizon_days == 28
    assert policy.human_evidence_hours == 400


def test_fit_applies_the_horizon_before_training():
    from tuner_under_test.engine.hybrid.engine import fit

    rows, spans = episodes(8)
    policy = EvidencePolicy(0, False, {}, human_evidence_hours=60 / 3600)
    got = fit(fit_problem(rows, spans, policy=policy))
    # Only the newest one-minute label remains: one class, so no fit.
    assert got.diagnostics["status"] == "insufficient"
    assert got.diagnostics["evidence"]["human_seconds"] == 60


def test_recording_window_beyond_the_labels_changes_nothing():
    from tuner_under_test.engine.hybrid.engine import fit

    rows, spans = episodes(12, seed=6)
    tight = fit(fit_problem(rows, spans))
    wide = fit(replace(fit_problem(rows, spans), start=-30 * DAY, end=40 * DAY))
    assert wide.thresholds == tight.thresholds
    assert wide.after == tight.after
    assert wide.before == tight.before
