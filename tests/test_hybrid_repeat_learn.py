"""A repeat Learn on real-shaped rows keeps its report while repeating as little work as it can."""

import json
from array import array
from pathlib import Path

import pytest
import test_tuner  # noqa: F401
from real_rows import recorded_view
from test_hybrid_learn_work import CASES, comparable, learn
from tuner_under_test.engine.hybrid import engine, exact, physical_index, replay
from tuner_under_test.engine.hybrid.types import RadarSpec
from tuner_under_test.history import cell_cache
from tuner_under_test.history.fit_days import FittingDays
from tuner_under_test.history.fit_rows import CapturedRowSource

BASELINE = Path(__file__).parent / "fixtures" / "hybrid_real_learn_parity.json"
ROWS = 100_000
CHANGED_LABEL_SECONDS = 600


def change_newest_label(view):
    view.data["devices"]["r"]["history_labels"][-1]["end"] -= CHANGED_LABEL_SECONDS


def recorded_reports(root, case):
    """A cold Learn's report and the report after the newest human label changed."""
    cold = learn(recorded_view(root, ROWS), case)
    view = recorded_view(root, ROWS)
    change_newest_label(view)
    return [json.loads(json.dumps(comparable(r), default=repr)) for r in (cold, learn(view, case))]


@pytest.fixture(scope="module")
def recording(tmp_path_factory):
    return tmp_path_factory.mktemp("real")


@pytest.mark.parametrize("name", CASES)
def test_real_shaped_learn_matches_the_recorded_baseline(recording, name):
    expected = json.loads(BASELINE.read_text(encoding="utf8"))[name]
    root = recording / name
    assert recorded_reports(root, CASES[name]) == expected


@pytest.fixture(scope="module")
def learned(recording):
    """A recording whose finished days were folded and cached by one cold Learn."""
    root = recording / "learned"
    learn(recorded_view(root, ROWS), CASES["plain"])
    return root


def _repeat(root):
    for memory in root.rglob("*.fit"):
        memory.unlink()
    view = recorded_view(root, ROWS)
    change_newest_label(view)
    return learn(view, CASES["plain"])


def _counted(monkeypatch, owner, name):
    calls, original = [], getattr(owner, name)

    def counted(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, name, counted)
    return calls


def test_repeat_learn_reads_cached_rows_without_compressing_or_validating_them(
    learned, monkeypatch
):
    spooled = _counted(monkeypatch, exact._Physical, "_write_block")
    validated = _counted(monkeypatch, physical_index.PhysicalRows, "validate")
    compiled = _counted(monkeypatch, physical_index._CompiledBlocks, "put")
    _repeat(learned)
    assert spooled == []
    assert len(validated) <= 2  # The open day, folded for this job and at the training cut.
    # A whole block costs more to compress than to decode again, and the sweep would evict it.
    assert all(len(call[2][0]) < physical_index.PHYSICAL_BLOCK_ROWS for call in compiled)


def test_repeat_learn_folds_the_open_day_at_most_once_for_each_cutoff_inside_it(
    learned, monkeypatch
):
    folds = _counted(monkeypatch, FittingDays, "_fold")
    _repeat(learned)
    assert len(folds) <= 2


def test_a_job_checksums_each_finished_day_once_however_many_sweeps_read_it(learned, monkeypatch):
    checked = _counted(monkeypatch, cell_cache, "_checksum")
    copied = _counted(monkeypatch, physical_index.PhysicalRows, "read")
    view = recorded_view(learned, ROWS)
    rows = CapturedRowSource(view, "r", tuple(range(18)))
    lo, hi = view._history_until - 3 * 86400, view._history_until
    first = rows.window(lo, hi)
    used, loads = len(checked), len(copied)
    assert CapturedRowSource(view, "r", tuple(range(18))).window(lo, hi) == first
    assert len(checked) == used
    assert len(copied) == loads


def test_streams_detecting_alike_find_their_runs_once_a_block():
    columns = {0: bytes((10, 90, 90, 10, 95, 10))}
    times = array("d", range(6))
    ends = array("d", range(1, 7))

    class Everything:
        def supports(self, _key, _value):
            return True

    radar = RadarSpec("r", (0,), {0: tuple(range(101))}, {0: 50}, {})
    tables = replay.support_tables(radar, Everything())
    state = replay._Radar(radar, [(50,), (60,), (92,)], tables)
    found = []
    original = replay.hits

    def counted(*args):
        found.append(args[0])
        return original(*args)

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(replay, "hits", counted)
        state.start()
        state.block(times, ends, columns)
    _, raw, active, supported = state.finish(6.0, True)
    assert len(found) == 2
    assert raw[0] == raw[1] == [(1.0, 3.0), (4.0, 5.0)]
    assert raw[2] == [(4.0, 5.0)]
    assert raw[0] is raw[1]
    assert active[0] is raw[0]
    assert supported[0] is raw[0]


def test_a_damaged_cached_block_rebuilds_its_day_once(learned, monkeypatch):
    view = recorded_view(learned, ROWS)
    rows = CapturedRowSource(view, "r", tuple(range(18)))
    lo, hi = view._history_until - 86400, view._history_until
    expected = rows.window(lo, hi)
    original = physical_index._decode_block
    damaged = []

    def decode(*args):
        if not damaged:
            damaged.append(1)
            raise ValueError("Invalid physical rows geometry")
        return original(*args)

    monkeypatch.setattr(physical_index, "_decode_block", decode)
    physical_index._DECODED.clear()
    physical_index._DECODED_BYTES[0] = 0
    monkeypatch.setattr(physical_index, "_COMPILED", physical_index._CompiledBlocks())
    folds = _counted(monkeypatch, FittingDays, "_fold")
    assert CapturedRowSource(view, "r", tuple(range(18))).window(lo, hi) == expected
    assert damaged == [1]
    assert len(folds) == 1


def test_presence_walk_prices_quiet_only_for_its_choice(learned, monkeypatch):
    walking, quiet, walks = [], [], []
    evaluate, walk = engine.evaluate_patterns, engine._presence_walk

    def evaluated(problem, patterns, plan, **options):
        if walking and options["quiet"]:
            quiet.append(len(patterns))
        return evaluate(problem, patterns, plan, **options)

    def walked(*args):
        walking.append(1)
        walks.append(1)
        try:
            return walk(*args)
        finally:
            walking.pop()

    monkeypatch.setattr(engine, "evaluate_patterns", evaluated)
    monkeypatch.setattr(engine, "_presence_walk", walked)
    _repeat(learned)
    assert walks
    assert sum(quiet) <= len(walks)
