"""Label anomaly audit: suspect sub-stretches, caps, and the masked re-score decision."""

import random
from dataclasses import replace

import pytest
import test_tuner  # noqa: F401
from test_hybrid_digest import block
from test_hybrid_evidence import truth as span
from test_hybrid_exact import Rows
from tuner_under_test.engine.hybrid import anomaly
from tuner_under_test.engine.hybrid.digest import fold_day
from tuner_under_test.engine.hybrid.engine import fit as engine_fit
from tuner_under_test.engine.hybrid.engine import prepared
from tuner_under_test.engine.hybrid.scoring import SliceScorer
from tuner_under_test.engine.hybrid.search import collect_training
from tuner_under_test.engine.hybrid.training import TrainingStore
from tuner_under_test.engine.hybrid.types import EvidencePolicy, FitProblem, RadarSpec, ZoneSpec

DAY = 86400
B = 1_790_000_000 // DAY * DAY
TIMING = {"timeout": 5, "on_delay": 0, "off_delay": 0}


class CachedDays:
    """One synthetic day, folded once."""

    def __init__(self, rows):
        self.rows = rows
        self.cache = {}
        self.reads = 0

    def dates(self, rid):
        return iter((0,))

    def bounds(self, date):
        return B, B + DAY

    def digest(self, rid, date, channels):
        self.reads += 1
        key = rid, tuple(channels)
        if key not in self.cache:
            digest = fold_day([block(self.rows)], B, B + DAY, channels)
            pieces = {cell: tuple(digest.partial.pieces(cell)) for cell in digest.partial}
            digest.partial.close()
            self.cache[key] = replace(digest, partial=pieces)
        return replace(self.cache[key], partial=dict(self.cache[key].partial))


def rows_for(segments, seed=1):
    """1 s rows; ``segments`` = [(seconds, kind)] with kind empty|busy|quiet.

    Channel 0 carries the signal (empty 0-9, busy 40-70, quiet 15-25: weak but
    above every empty reading); the other channels are 0-9 noise in every state.
    """
    rng = random.Random(seed)
    rows, spans, t = [], [], float(B + 3600)
    for seconds, kind in segments:
        spans.append((t, t + seconds, kind))
        for _ in range(int(seconds)):
            values = []
            for c in range(18):  # the prototype fixture's exact draw order
                if c == 0 and kind == "busy":
                    values.append(rng.randint(40, 70))
                elif c == 0 and kind == "quiet":
                    values.append(rng.randint(15, 25))
                else:
                    values.append(rng.randint(0, 9))
            rows.append((t, t + 1, values))
            t += 1
    return rows, spans


def label(spans, i, state, source="human", lo=0.0, hi=0.0, episode=None):
    a, b, _ = spans[i]
    return span(a + lo, b - hi, state, source, 1.0, episode or f"{i}-{state}-{source}-{lo}")


def problem_for(rows, truth, start=50):
    channels = tuple(range(18))
    radar = RadarSpec(
        "a",
        channels,
        dict.fromkeys(channels, tuple(range(101))),
        dict.fromkeys(channels, start),
        TIMING,
    )
    return FitProblem(
        {"a": radar},
        (ZoneSpec("room", ("a",), tuple(truth)),),
        rows[0][0],
        rows[-1][1],
        CachedDays(rows),
        {"a": Rows(rows)},
        EvidencePolicy(0.5, False, {}),
    )


def audit(problem, tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    problem = prepared(problem)
    with TrainingStore(tmp_path) as store:
        collect_training(problem, store)
        found = anomaly.audit(problem, store)
    return found.accepted, found.capped


def base_segments():
    # empty 40 min | busy 30 min | empty 40 min | busy 30 min | empty 40 min
    return [(2400, "empty"), (1800, "busy"), (2400, "empty"), (1800, "busy"), (2400, "empty")]


def clean_truth(spans, segments):
    return [
        label(spans, i, "present" if kind == "busy" else "not_present")
        for i, (_, kind) in enumerate(segments)
    ]


def fit(rows, truth, audit_on=True):
    result = engine_fit(problem_for(rows, truth), audit=audit_on)
    return result.thresholds["a"], result


def phone_case():
    """A phone left behind: entity Present for 30 of each empty stretch's 40 minutes.

    Occupancy evidence comes from the entity (a presence sensor) alone, so that
    class stays primary. With human Present labels the production evidence policy
    already caps the entity's influence and the corruption cannot block the fit.
    """
    rows, spans = rows_for(base_segments())
    return rows, [
        label(spans, 0, "not_present", hi=1800),
        label(spans, 0, "present", "entity", lo=600),
        label(spans, 1, "present", "entity"),
        label(spans, 2, "not_present", hi=1800),
        label(spans, 2, "present", "entity", lo=600),
        label(spans, 3, "present", "entity"),
        label(spans, 4, "not_present"),
    ]


def phone_clean(spans):
    return [
        label(spans, i, "present", "entity") if kind == "busy" else label(spans, i, "not_present")
        for i, (_, kind) in enumerate(base_segments())
    ]


def test_empty_like_stretch_inside_present_label_is_a_suspect_sub_stretch(tmp_path):
    segs = [(2400, "empty"), (2400, "busy"), (600, "empty"), (2400, "busy"), (2400, "empty")]
    rows, spans = rows_for(segs)
    truth = [
        label(spans, 0, "not_present"),
        span(spans[1][0], spans[3][1], "present", episode="long"),
        label(spans, 4, "not_present"),
    ]
    accepted, capped = audit(problem_for(rows, truth), tmp_path)
    assert not capped
    assert len(accepted) == 1
    s = accepted[0]
    assert s["state"] == "present" and s["source"] == "human" and s["zone"] == "room"
    assert spans[2][0] - 60 <= s["start"] and s["end"] <= spans[2][1] + 60
    assert 300 <= s["seconds"] <= 660
    assert s["reason"] == anomaly.REASON["present"]


def test_quiet_presence_is_never_a_suspect(tmp_path):
    segs = [(2400, "empty"), (600, "busy"), (2400, "quiet"), (600, "busy"), (2400, "empty")]
    rows, spans = rows_for(segs)
    truth = [
        label(spans, 0, "not_present"),
        span(spans[1][0], spans[3][1], "present", episode="long"),
        label(spans, 4, "not_present"),
    ]
    assert audit(problem_for(rows, truth), tmp_path) == ([], [])


def test_quiet_presence_window_is_not_empty_like():
    cells = 10
    windows = [(0, 0, cells, 0.0, 60.0)]
    quiet = bytes([20] * cells)  # weak, but every cell clears the robust empty bound
    ref = {("a", 0): {"empty_bound": 9}, ("a", 1): {"empty_bound": 9}}
    columns = {("a", 0): quiet, ("a", 1): bytes([5] * cells)}
    assert anomaly.empty_like(windows, ref, lambda key, side: columns[key]) == []
    stray = bytearray([5] * cells)
    stray[3] = 20  # one stray cell above the bound is still empty-like
    columns["a", 0] = bytes(stray)
    assert anomaly.empty_like(windows, ref, lambda key, side: columns[key]) == windows


def test_occupied_like_stretch_inside_empty_label_is_a_suspect(tmp_path):
    segs = [(2400, "empty"), (900, "busy"), (1200, "empty"), (900, "busy"), (1200, "empty")]
    rows, spans = rows_for(segs)
    truth = [
        label(spans, 0, "not_present"),
        label(spans, 1, "not_present"),
        label(spans, 2, "not_present"),
        label(spans, 3, "present"),
        label(spans, 4, "not_present"),
    ]
    accepted, _ = audit(problem_for(rows, truth), tmp_path)
    assert accepted and all(s["state"] == "not_present" for s in accepted)
    assert all(spans[1][0] - 60 <= s["start"] and s["end"] <= spans[1][1] + 60 for s in accepted)
    assert sum(s["seconds"] for s in accepted) >= 0.3 * 900


def test_human_needs_longer_stretches_than_entity(tmp_path):
    segs = [(2400, "empty"), (1200, "busy"), (120, "empty"), (1200, "busy"), (2400, "empty")]
    rows, spans = rows_for(segs)
    empties = [label(spans, 0, "not_present"), label(spans, 4, "not_present")]
    entity = [*empties, span(spans[1][0], spans[3][1], "present", "entity", 1.0, "p")]
    found, _ = audit(problem_for(rows, entity), tmp_path / "e")
    assert found and all(s["source"] == "entity" for s in found)
    assert anomaly.MIN_WINDOWS[("present", "entity")] < anomaly.MIN_WINDOWS[("present", "human")]
    assert (
        anomaly.MIN_WINDOWS[("not_present", "entity")]
        < anomaly.MIN_WINDOWS[("not_present", "human")]
    )


def suspects(n, source):
    return [
        {
            "zone": "z",
            "start": float(i * 100),
            "end": float(i * 100 + 100),
            "state": "present",
            "source": source,
            "seconds": 100.0,
        }
        for i in range(n)
    ]


def test_caps_keep_every_class_and_limit_human_discards():
    training = {"present": 1000.0, "not_present": 1000.0}
    human = {"present": 1000.0, "not_present": 1000.0}
    accepted, capped = anomaly.limit(suspects(9, "human"), training, human)
    assert sum(s["seconds"] for s in accepted) <= anomaly.CAP_HUMAN * 1000.0
    assert len(accepted) + len(capped) == 9 and capped
    accepted, _ = anomaly.limit(suspects(9, "entity"), training, human)
    assert sum(s["seconds"] for s in accepted) <= anomaly.CAP_CLASS * 1000.0


def test_class_never_vanishes_under_caps():
    training = {"present": 600.0, "not_present": 600.0}
    accepted, capped = anomaly.limit(suspects(6, "entity"), training, dict.fromkeys(training, 0.0))
    kept = training["present"] - sum(s["seconds"] for s in accepted)
    assert kept >= (1 - anomaly.CAP_CLASS) * training["present"] > 0
    assert capped
    # Entity suspects go first, then longer, so human evidence is kept preferentially.
    mixed = suspects(3, "human") + suspects(3, "entity")
    order = [s["source"] for s in anomaly.limit(mixed, {"present": 1e9}, {"present": 1e9})[0]]
    assert order.count("entity") == 3


def test_mask_keeps_label_identity_and_scores_only_retained_time(tmp_path):
    zone = ZoneSpec(
        "z", ("a",), (span(0, 100, episode="p"), span(100, 200, "not_present", episode="e"))
    )
    with SliceScorer(zone, EvidencePolicy(0, False, {}), (0, 200), tmp_path, mask=[(40, 60)]) as s:
        s.consume(
            0, 200, {"a": [(0, 200)]}, {"a": [(40, 60)]}, {"a": [(40, 60)]}, {"a": [(40, 60)]}
        )
        r = s.result()
    assert r["occupied_seconds"] == 80 and r["occupied_recall"] == 0
    assert r["occupied_episodes"] == 1 and r["mostly_missed_episodes"] == 1
    assert r["uncovered_label_seconds"] == 0


def test_masked_time_is_unobserved_for_fitter_and_judge(tmp_path):
    from tuner_under_test.engine.hybrid.search import statistics

    rows, spans = rows_for([(600, "empty"), (600, "busy"), (600, "empty")])
    truth = [
        label(spans, 0, "not_present"),
        label(spans, 1, "present"),
        label(spans, 2, "not_present"),
    ]
    p = prepared(problem_for(rows, truth))
    masks = {"room": [(spans[1][0], spans[1][1])]}
    masked = anomaly.masked_problem(p, masks)
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        seen = statistics(masked, store, masks=masks)[("a", 0)]
    assert not any(seen["hp"])  # the fitter's statistics lose the masked present cells
    assert any(seen["he"])
    from tuner_under_test.engine.hybrid.exact import exact_scores, regions

    judged = exact_scores(masked, [{"a": dict.fromkeys(range(18), 30)}], regions(p.zones[0].truth))
    assert judged[0]["occupied_seconds"] == 0  # never flipped to empty, just unobserved
    assert judged[0]["empty_seconds"] == 1200


def test_human_labels_win_over_overlapping_entity_labels():
    from tuner_under_test.engine.hybrid.evidence import _labels

    truth = (
        span(0, 100, "not_present", episode="h"),
        span(50, 150, "present", "entity", episode="e"),
    )
    assert [(a, b, s.state, s.source) for a, b, s in _labels(truth)] == [
        (0, 100, "not_present", "human"),
        (100, 150, "present", "entity"),
    ]


def test_clean_labels_are_left_alone_and_match_the_unaudited_fit():
    rows, spans = rows_for(base_segments())
    truth = clean_truth(spans, base_segments())
    robust, report = fit(rows, truth)
    plain, _ = fit(rows, truth, audit_on=False)
    assert robust == plain
    assert report.diagnostics["anomaly"]["status"] == "clean"


def test_phone_left_behind_entity_present_over_empty_is_discarded_and_fit_recovers():
    rows, corrupt = phone_case()
    _, spans = rows_for(base_segments())
    good, _ = fit(rows, phone_clean(spans))
    bad, _ = fit(rows, corrupt, audit_on=False)
    robust, report = fit(rows, corrupt)
    a = report.diagnostics["anomaly"]
    assert a["status"] == "discarded"
    assert bad != good
    assert robust == good
    rows_ = a["discarded"]
    assert rows_ and all(r["source"] == "entity" and r["state"] == "present" for r in rows_)
    assert all(r["decision"] == "discarded" and r["reason"] for r in rows_)
    by = a["discarded_seconds"]["present/entity"]
    assert 2 * 1500 <= by["seconds"] <= 2 * 1800 and by["stretches"] == 2
    assert a["gain"] > a["margin"]
    assert report.diagnostics["first_fit"]["status"] in ("fitted", "kept")


def test_suspects_without_influence_are_kept_and_reported():
    rows, spans = rows_for(base_segments())
    truth = [
        label(spans, 0, "not_present"),
        span(spans[1][0], spans[1][1] + 240, "present", episode="p1"),
        span(spans[2][0] + 240, spans[2][1], "not_present", episode="e1"),
        label(spans, 3, "present"),
        label(spans, 4, "not_present"),
    ]
    robust, report = fit(rows, truth)
    plain, _ = fit(rows, truth, audit_on=False)
    a = report.diagnostics["anomaly"]
    assert a["suspects"] >= 1
    if a["status"] != "discarded":
        assert robust == plain
        assert a["status"] == "kept: no influence"
        assert a["kept"] and all(r["decision"] == "kept: no influence" for r in a["kept"])


def test_no_influence_keeps_first_fit(monkeypatch):
    rows, truth = phone_case()
    plain = engine_fit(problem_for(rows, truth), audit=False)
    monkeypatch.setattr(anomaly, "evidence_margin", lambda *_: float("inf"))
    kept = engine_fit(problem_for(rows, truth))
    a = kept.diagnostics.pop("anomaly")
    assert a["status"] == "kept: no influence"
    assert a["discarded_count"] == 0 and not a["discarded"]
    assert kept.thresholds == plain.thresholds
    assert kept.after == plain.after
    assert kept.before == plain.before


def test_audit_never_reads_beyond_the_given_labels(tmp_path):
    segs = base_segments() + [(1200, "busy"), (1200, "empty")]
    rows, spans = rows_for(segs)
    truth = clean_truth(spans, segs[:5])
    assert audit(problem_for(rows, truth), tmp_path) == ([], [])


def test_audit_never_reads_heldout_labels(tmp_path):
    # Held-out evidence never enters the FitProblem; the audit reads only the
    # training store built from the problem's own labels.
    segs = base_segments() + [(1200, "busy"), (1200, "empty")]
    rows, spans = rows_for(segs)
    training = clean_truth(spans, segs[:5])
    heldout = [label(spans, 5, "not_present"), label(spans, 6, "present")]  # contradicts
    p = prepared(problem_for(rows, training + heldout))
    q = prepared(problem_for(rows, training))
    with TrainingStore(tmp_path) as store:
        collect_training(q, store)
        found = anomaly.audit(q, store)
        found_again = anomaly.audit(replace(q, zones=p.zones), store)
    assert found.accepted == []
    assert all(s["end"] <= spans[4][1] for s in found_again.accepted)


@pytest.mark.parametrize("kind", ["busy", "quiet"])
def test_audit_is_deterministic(kind, tmp_path):
    segs = [(2400, "empty"), (900, kind), (600, "empty"), (900, "busy"), (2400, "empty")]
    rows, spans = rows_for(segs)
    truth = [label(spans, 0, "not_present"), span(spans[1][0], spans[3][1], "present", episode="p")]
    p = problem_for(rows, truth)
    assert audit(p, tmp_path / "1") == audit(p, tmp_path / "2")


def test_sub_window_remnants_at_label_edges_go_with_the_discarded_stretch(tmp_path):
    segs = [(2400, "empty"), (2400, "busy"), (2400, "empty"), (2400, "busy"), (2400, "empty")]
    rows, spans = rows_for(segs)
    a = spans[2][0] + 601.5
    truth = [
        label(spans, 0, "not_present"),
        label(spans, 1, "present"),
        span(spans[2][0], a, "not_present", episode="e2"),
        span(a, a + 630.0, "present", "entity", 1.0, "fake"),
        span(a + 630.0, spans[2][1], "not_present", episode="e2"),
        label(spans, 3, "present"),
        label(spans, 4, "not_present"),
    ]
    accepted, _ = audit(problem_for(rows, truth), tmp_path)
    mine = [s for s in accepted if s["source"] == "entity"]
    assert len(mine) == 1
    assert (mine[0]["start"], mine[0]["end"]) == (a, a + 630.0)


def test_discard_is_a_substretch_never_whole_label(tmp_path):
    segs = [(2400, "empty"), (2400, "busy"), (600, "empty"), (2400, "busy"), (2400, "empty")]
    rows, spans = rows_for(segs)
    whole = span(spans[1][0], spans[3][1], "present", episode="long")
    truth = [label(spans, 0, "not_present"), whole, label(spans, 4, "not_present")]
    accepted, _ = audit(problem_for(rows, truth), tmp_path)
    assert accepted
    for s in accepted:
        assert whole.start < s["start"] and s["end"] < whole.end
        assert s["seconds"] < 0.25 * (whole.end - whole.start)


def windows(pattern, gap_at=()):
    """Fake windows (side, lo, hi, t0, t1) of 60 s; ``pattern`` "F" flagged, "." not."""
    out, t = [], 0.0
    for i, _ in enumerate(pattern):
        if i in gap_at:
            t += 30.0
        out.append((0, i * 10, i * 10 + 10, t, t + 60.0))
        t += 60.0
    return out, {w for w, ch in zip(out, pattern, strict=True) if ch == "F"}


def test_groups_bridge_unflagged_windows_while_the_flagged_share_holds():
    wins, flagged = windows("FFFFF..FFFFFF.FFFF.F")
    assert [len(g) for g in anomaly._groups(wins, flagged)] == [20]


def test_groups_never_bridge_into_a_long_unflagged_run_or_a_coverage_break():
    wins, flagged = windows("FFFF........FFFF")
    assert [len(g) for g in anomaly._groups(wins, flagged)] == [4, 4]
    wins, flagged = windows("FFFFFFFF", gap_at=(4,))
    assert [len(g) for g in anomaly._groups(wins, flagged)] == [4, 4]


def test_groups_merge_small_and_large_runs_when_the_merged_share_holds():
    wins, flagged = windows("..FF.." + "F" * 25)
    assert [len(g) for g in anomaly._groups(wins, flagged)] == [31]
    wins, flagged = windows("....FF" + "F" * 25)
    assert [len(g) for g in anomaly._groups(wins, flagged)] == [27]


def test_cpu_budget_reports_not_tested(monkeypatch):
    # The prototype's CPU guard is now a deterministic row-pattern operation budget.
    rows, truth = phone_case()
    plain, _ = fit(rows, truth, audit_on=False)
    monkeypatch.setattr(anomaly, "REFIT_OPERATIONS", 0)
    robust, report = fit(rows, truth)
    a = report.diagnostics["anomaly"]
    assert robust == plain
    assert a["status"] == "kept: not tested (operation budget)"
    assert a["kept"] and not a["discarded"]
    assert a["operations"] > a["operation_budget"] == 0


def test_masked_audit_never_repeats_coordinate_search(monkeypatch):
    from tuner_under_test.engine.hybrid import replay, search

    rows, truth = phone_case()
    runs, original_run, original_replay = [], search._Search.run, replay.replay
    audited = []

    def checked(self, *args, **kwargs):
        assert not audited, "masked audit repeated coordinate search"
        return original_run(self, *args, **kwargs)

    original_audit = anomaly.audit

    def mark(*args, **kwargs):
        audited.append(True)
        return original_audit(*args, **kwargs)

    def counted(problem, providers, regions, wanted, *args):
        runs.append((bool(audited), sum(len(v) for v in wanted.values())))
        return original_replay(problem, providers, regions, wanted, *args)

    monkeypatch.setattr(search._Search, "run", checked)
    monkeypatch.setattr(anomaly, "audit", mark)
    monkeypatch.setattr("tuner_under_test.engine.hybrid.exact.replay", counted)
    _, report = fit(rows, truth)
    assert report.diagnostics["anomaly"]["status"] == "discarded"
    # Fit-1 replay records are reused: the re-score replays only the two new
    # closed-form masked seeds, each with its quiet (raised-threshold) pass.
    first = sum(n for after, n in runs if not after)
    later = sum(n for after, n in runs if after)
    assert first > later
    assert later <= 4


def test_hardware_speed_never_changes_anomaly_choice(monkeypatch):
    import time

    rows, truth = phone_case()
    expected, report = fit(rows, truth)
    clock = iter(range(0, 10**9, 1000))
    monkeypatch.setattr(time, "process_time", lambda: next(clock))
    monkeypatch.setattr(time, "perf_counter", lambda: next(clock))
    actual, slow = fit(rows, truth)
    assert actual == expected
    assert slow.diagnostics["anomaly"] == report.diagnostics["anomaly"]


def test_discarding_is_deterministic():
    rows, truth = phone_case()
    first = fit(rows, truth)
    second = fit(rows, truth)
    assert first[0] == second[0]
    assert first[1] == second[1]


class TwoRadarDays(CachedDays):
    def __init__(self, rows):
        super().__init__(rows["a"])
        self.other = CachedDays(rows["b"])

    def digest(self, rid, date, channels):
        source = self.other if rid == "b" else super()
        return source.digest(rid, date, channels)


def test_audit_member_groups_match_one_buffer(tmp_path, monkeypatch):
    segs = [(2400, "empty"), (900, "busy"), (1200, "empty"), (900, "busy"), (1200, "empty")]
    rows, spans = rows_for(segs)
    other, _ = rows_for(segs, seed=2)
    truth = [label(spans, i, "not_present") for i in range(3)]
    truth += [label(spans, 3, "present"), label(spans, 4, "not_present")]
    one = problem_for(rows, truth)
    p = replace(
        one,
        radars={**one.radars, "b": replace(one.radars["a"], device_id="b")},
        zones=(ZoneSpec("room", ("a", "b"), tuple(truth)),),
        days=TwoRadarDays({"a": rows, "b": other}),
        rows={"a": Rows(rows), "b": Rows(other)},
    )
    whole = audit(p, tmp_path / "whole")
    monkeypatch.setattr(anomaly, "AUDIT_BUFFER_BYTES", 1)  # one member per group
    grouped = audit(p, tmp_path / "grouped")
    assert whole[0]
    assert grouped == whole
