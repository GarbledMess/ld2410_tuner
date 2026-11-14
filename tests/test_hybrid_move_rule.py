"""Move thresholds settle inside the gap between empty-room move noise and arrival bursts."""

import random

import test_tuner  # noqa: F401
from test_hybrid_digest import block
from tuner_under_test.engine.hybrid.digest import fold_day
from tuner_under_test.engine.hybrid.move_rule import move_thresholds
from tuner_under_test.engine.hybrid.types import FitProblem, RadarSpec, TruthSpan, ZoneSpec

HOUR = 3600.0
DOMAIN = tuple(range(101))


def span(lo, hi, state, source="human"):
    return TruthSpan(lo, hi, state, source, 1.0, f"{state}-{lo}", ())


def cycles(count=6, empty=2 * HOUR, present=HOUR, noise=20, burst=80, still=60, seed=4, step=1):
    """Rows and truth: empty stretches (move noise up to ``noise``), then arrivals whose first
    30 s burst to ``burst`` on move gate 0 while still gate 0 holds ``still``."""
    rng = random.Random(seed)
    rows, truth, t = [], [], 0.0
    for _ in range(count):
        for s in range(0, int(empty), step):
            values = [0] * 18
            values[0] = rng.randint(5, noise)
            values[2] = rng.randint(5, 30)  # move gate 1: noisy, never rises at arrivals
            values[1] = 5
            rows.append((t + s, t + s + step, values))
        truth.append(span(t, t + empty, "not_present"))
        t += empty
        for s in range(0, int(present), step):
            values = [0] * 18
            values[0] = burst if s < 30 else rng.randint(5, noise)
            values[2] = rng.randint(5, 30)
            values[1] = still
            rows.append((t + s, t + s + step, values))
        truth.append(span(t, t + present, "present"))
        t += present
    return rows, truth


class Days:
    """One recorded "day" spanning the rows, folded as the engine folds a real day."""

    def __init__(self, rows, channels):
        self.folded = fold_day([block(rows)], rows[0][0], rows[-1][1], channels)

    def dates(self, _radar):
        return iter([0])

    def digest(self, _radar, _date, _channels):
        return self.folded


def problem(rows, truth, channels=(0, 1, 2, 3), current=None, domains=None, zones=None):
    current = current or dict.fromkeys(channels, 50)
    domains = domains or dict.fromkeys(channels, DOMAIN)
    radar = RadarSpec("r", tuple(channels), domains, current, {})
    zones = zones or (ZoneSpec("z", ("r",), tuple(truth)),)
    days = Days(rows, tuple(channels))
    return FitProblem({"r": radar}, zones, rows[0][0], rows[-1][1], days, {}, None)


def test_move_settles_a_third_into_the_gap():
    rows, truth = cycles()
    found = move_thresholds(problem(rows, truth))["r"]
    assert found[0] == 40  # noise 20, arrivals 80: 20 + (80 - 20) / 3


def test_gate_that_never_rises_at_arrivals_is_switched_off():
    rows, truth = cycles()
    assert move_thresholds(problem(rows, truth))["r"][2] == 100


def test_still_channels_are_left_to_the_still_fit():
    rows, truth = cycles()
    assert set(move_thresholds(problem(rows, truth))["r"]) == {0, 2}


def test_threshold_rounds_up_to_an_allowed_value():
    rows, truth = cycles()
    domains = dict.fromkeys((0, 1, 2, 3), (0, 25, 45, 100))
    assert move_thresholds(problem(rows, truth, domains=domains))["r"][0] == 45


def test_automatic_empty_edges_are_not_trusted():
    rows, truth = cycles()
    # The automatic guess flips to present 60 s late: the arrival burst falls inside its empty
    # span. Trimming the guess's edges keeps the burst out of the empty-room noise.
    late = []
    for s in truth:
        if s.state == "not_present":
            late.append(span(s.start, s.end + 60, "not_present", "automatic"))
        else:
            late.append(span(s.start + 60, s.end, "present", "automatic"))
    found = move_thresholds(problem(rows, late))["r"]
    assert found[0] < 80


def test_automatic_piece_that_looks_occupied_is_dropped_but_human_is_kept():
    rows, truth = cycles()
    # A sustained still reading in the first empty stretch: a person the guess missed.
    polluted = []
    for t, e, v in rows:
        if 600 <= t < 1800:
            v = list(v)
            v[1], v[0] = 70, 90
        polluted.append((t, e, v))
    automatic = [span(s.start, s.end, s.state, "automatic") for s in truth]
    assert move_thresholds(problem(polluted, automatic))["r"][0] == 40
    # A human says the same stretch was empty (the dog): its energy is real empty-room noise.
    assert move_thresholds(problem(polluted, truth))["r"][0] > 40


def test_human_empty_sets_the_noise_even_when_rare_among_automatic_pieces():
    rows, truth = cycles(count=120, empty=HOUR, present=HOUR / 4, step=5)
    # Automatic guesses everywhere, except one human-labelled empty hour in which the dog
    # moves (60): rare among ~240 pieces, but a human said it is empty, so move stays above it.
    labels = [span(s.start, s.end, s.state, "automatic") for s in truth]
    first = truth[0]
    labels[0] = span(first.start, first.end, "not_present", "human")
    dog = [(t, e, ([60] + list(v[1:])) if 1200 <= t < 1500 else v) for t, e, v in rows]
    assert move_thresholds(problem(dog, labels))["r"][0] == 67  # 60 + (80 - 60) / 3


def test_no_empty_evidence_keeps_current_move():
    rows, truth = cycles()
    only_present = [s for s in truth if s.state == "present"]
    assert move_thresholds(problem(rows, only_present)) == {}


def test_one_polluted_piece_does_not_move_the_noise():
    rows, truth = cycles(count=120, empty=HOUR, present=HOUR / 4, step=5)
    spiked = [(t, e, ([90] + list(v[1:])) if t == 100 else v) for t, e, v in rows]
    assert move_thresholds(problem(spiked, truth))["r"][0] == 40


def test_room_radar_counts_empty_only_where_every_zone_it_covers_is_empty():
    rows, truth = cycles()
    other = []
    for s in truth:  # the second zone is occupied for the first hour of each empty stretch
        if s.state == "not_present":
            other.append(span(s.start, s.start + HOUR, "present"))
            other.append(span(s.start + HOUR, s.end, "not_present"))
        else:
            other.append(s)
    busy = []
    for t, e, v in rows:  # the second zone's occupant moves within this radar's view
        if any(s.state == "present" and s.start <= t < s.end for s in other[::3]):
            v = list(v)
            v[0] = max(v[0], 70)
        busy.append((t, e, v))
    zones = (ZoneSpec("a", ("r",), tuple(truth)), ZoneSpec("b", ("r",), tuple(other)))
    assert move_thresholds(problem(busy, truth, zones=zones))["r"][0] == 40


def two_gate_view(tmp_path, samples, labels):
    """A worker-restored view over one recorded day of ``(move, still)`` 6-second samples."""
    from types import SimpleNamespace

    from tuner_under_test.history import dayfile
    from tuner_under_test.worker import history
    from tuner_under_test.worker.calculations import ReadOnlyDays

    builder = dayfile.DayBuilder(0, 86400)
    for i, (move, still) in enumerate(samples):
        values = {0: move, 1: still, **dict.fromkeys(range(2, 18), 255)}
        builder.add(dayfile.Change(i * 6, values))
    end = len(samples) * 6
    builder.add(dayfile.End(end))
    path = tmp_path / "r.day"
    path.write_bytes(dayfile.encode_day(builder.day()))
    entry = {
        "device_id": "r",
        "date": "1970-01-01",
        "path": str(path),
        "identity": history._identity(path.stat()),
        "late": [],
        "persistent": False,
    }
    budgets = {"max_file_bytes": None, "max_raw_bytes": 1 << 30, "max_rows": None}
    days = ReadOnlyDays({"days": [entry], "budgets": {**budgets, "max_guesses": None}})
    device = {"history_labels": list(labels), "training_state": "unknown"}
    return SimpleNamespace(
        data={"devices": {"r": device}, "room_snapshot": None},
        hass=None,
        _days=days,
        _history_until=end,
        _attribution_until=end,
    )


def arrivals_day(count=6, empty=1800, present=900, seed=7):
    """Empty stretches (move noise 10..20, still 5); arrivals burst move to 80 for 30 s while
    still holds 60 for the whole stay."""
    rng = random.Random(seed)
    samples, labels, t = [], [], 0
    for _ in range(count):
        samples += [(rng.randint(10, 20), 5) for _ in range(empty // 6)]
        labels.append({"start": t, "end": t + empty, "state": "not_present"})
        t += empty
        samples += [(80 if i < 5 else rng.randint(10, 20), 60) for i in range(present // 6)]
        labels.append({"start": t, "end": t + present, "state": "present"})
        t += present
    return samples, labels


def test_learn_sets_move_from_the_gap_and_still_by_search(tmp_path):
    from tuner_under_test.engine.hybrid import facade

    samples, labels = arrivals_day()
    view = two_gate_view(tmp_path, samples, labels)
    report = facade.fit(
        view, "r", ["g0_move", "g0_still"], {"g0_move": 50, "g0_still": 50}, None, 0.0
    )
    found = {key: p["threshold"] for key, p in report["proposals"].items()}
    assert found["g0_move"] == 40  # noise 20, arrivals 80, a third of the way
    assert 5 <= found["g0_still"] < 60  # the still search separates empty from seated


def test_without_arrivals_presence_move_stands_in_for_the_burst():
    # Labels begin mid-presence: no arrival follows labelled empty time. Move reads 50 while
    # people are there and up to 20 when empty: the gap is still there to settle into.
    rng = random.Random(3)
    rows, truth, t = [], [], 0.0
    for state, level in (("present", 50), ("not_present", 0)) * 3:
        for s in range(0, 3600, 5):
            values = [0] * 18
            values[0] = level or rng.randint(5, 20)
            values[1] = 60 if state == "present" else 5
            rows.append((t + s, t + s + 5, values))
        truth.append(span(t, t + 3600, state))
        t += 7200  # a recording gap: no arrival is ever seen
    assert move_thresholds(problem(rows, truth))["r"][0] == 30  # 20 + (50 - 20) / 3


def test_move_coverage_marks_presence_only_move_sees_plus_hold():
    from tuner_under_test.engine.hybrid.move_rule import move_plan

    # Still reads its empty level (5) while present: the arrival burst is move's alone.
    rows, truth = cycles(count=1, still=5)
    p = problem(rows, truth)
    plan = move_plan(p)
    assert plan.thresholds["r"] == {0: 40, 2: 100}
    assert plan.coverage["r"] == [(7200.0, 7230.0)]  # 30 s at 80, 6-second cells
    p.radars["r"] = RadarSpec(
        "r",
        (0, 1, 2, 3),
        dict.fromkeys(range(4), DOMAIN),
        dict.fromkeys(range(4), 50),
        {"timeout": 10},
    )
    assert move_plan(p).coverage["r"] == [(7200.0, 7240.0)]


def test_presence_still_also_sees_is_not_move_coverage():
    from tuner_under_test.engine.hybrid.move_rule import move_plan

    rows, truth = cycles(count=1)  # still holds 60 through the stay, burst included
    p = problem(rows, truth)
    assert move_plan(p).coverage["r"] == []


def test_still_truth_drops_presence_only_move_catches():
    from tuner_under_test.engine.hybrid.move_rule import move_plan, still_truth

    rows, truth = cycles(count=1, still=5)
    p = problem(rows, truth)
    found = still_truth(p, move_plan(p))
    present = [(s.start, s.end) for s in found.zones[0].truth if s.state == "present"]
    empty = [(s.start, s.end) for s in found.zones[0].truth if s.state == "not_present"]
    assert present == [(7230.0, 10800.0)]
    assert empty == [(0.0, 7200.0)]


def test_empty_noise_reads_only_cells_wholly_inside_the_label():
    rows, truth = cycles()
    # Labels whose edges fall mid-cell: the cell straddling each empty start also holds the
    # end of the previous stay (the arrival burst is long over; move there reads 20 at most),
    # and the cell straddling each empty end holds the next arrival's burst (80).
    shifted = [span(s.start + 3, s.end + 3, s.state) for s in truth]
    assert move_thresholds(problem(rows, shifted))["r"][0] == 40
