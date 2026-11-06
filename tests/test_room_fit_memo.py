"""A room fit over unchanged inputs is done once; any changed input is a different fit."""

import copy
import os
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
import test_tuner  # noqa: F401
from room_speed_fixture import GROUP, RADARS, build, request
from tuner_under_test.engine.hybrid import adapters, engine, fit_memo
from tuner_under_test.history import fit_rows, worker_snapshot
from tuner_under_test.rooms import adaptive
from tuner_under_test.worker import calculations, codec
from tuner_under_test.worker.manifest import expand


@pytest.fixture(scope="module")
def executor():
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(worker_snapshot, "_executor_only", lambda: None)
        yield


@pytest.fixture(scope="module")
def store(tmp_path_factory, executor):
    root = tmp_path_factory.mktemp("memo")
    return build(root, days=7, open_seconds=1800, sensor_days=2)


def _forget(store):
    for path in Path(store.store.root).rglob("*.fit"):
        path.unlink()


def _job(store, folder, labels=None, **options):
    fixture = copy.copy(store)
    if labels is not None:
        fixture.labels = labels
    return request(fixture, folder, **{"window_days": 6, "attribution_days": 1, **options})


def _shorter_newest(store):
    """The labels with the newest human one ten minutes shorter."""
    changed = [dict(label) for label in store.labels]
    manual = (label for label in changed if label["source"] == "manual")
    max(manual, key=lambda label: label["start"])["end"] -= 600
    return changed


def _inputs(job):
    """What a worker's ``_room`` gets: the job's views and parameters."""
    expanded = expand(job)
    shared = calculations.JobHistory()
    views = {key: calculations.restore(value, shared) for key, value in expanded["views"].items()}
    return views, codec.decode(job["parameters"])


def _counted(owner, name, calls):
    original = getattr(owner, name)

    def counted(*args, **kwargs):
        calls.append(name)
        return original(*args, **kwargs)

    return patch.object(owner, name, counted)


def _execute(job):
    calls = []
    with (
        _counted(engine, "fit", calls),
        _counted(adaptive, "collect_contexts", calls),
        _counted(fit_rows.CapturedRowSource, "blocks", calls),
    ):
        report = calculations.execute(job)
    return report, calls


def test_a_repeat_with_nothing_changed_reads_no_row_and_fits_nothing(store, tmp_path):
    _forget(store)
    first, calls = _execute(_job(store, tmp_path / "one"))
    assert calls.count("fit") >= 1
    # A later job: captured again in its own folder, as the integration does.
    second, calls = _execute(_job(store, tmp_path / "two"))
    assert calls == []
    assert codec.pack_result(second) == codec.pack_result(first)


def test_a_changed_label_is_not_served_the_remembered_report(store, tmp_path):
    _forget(store)
    first, _ = _execute(_job(store, tmp_path / "one"))
    changed = _shorter_newest(store)
    second, calls = _execute(_job(store, tmp_path / "two", labels=changed))
    assert "collect_contexts" in calls
    _forget(store)
    fresh, _ = _execute(_job(store, tmp_path / "three", labels=changed))
    assert codec.pack_result(second) == codec.pack_result(fresh)
    assert codec.pack_result(second) != codec.pack_result(first)


def test_a_changed_newest_label_reuses_the_training_fits(store, tmp_path):
    _forget(store)
    _execute(_job(store, tmp_path / "one"))
    changed = _shorter_newest(store)
    second, calls = _execute(_job(store, tmp_path / "two", labels=changed))
    fits = calls.count("fit")
    _forget(store)
    fresh, calls = _execute(_job(store, tmp_path / "three", labels=changed))
    assert fits < calls.count("fit") == 3
    assert codec.pack_result(second) == codec.pack_result(fresh)


def test_a_damaged_memory_is_fitted_again(store, tmp_path):
    _forget(store)
    first, _ = _execute(_job(store, tmp_path / "one"))
    for path in Path(store.store.root).rglob("*.fit"):
        path.write_bytes(b"not a memory")
    second, calls = _execute(_job(store, tmp_path / "two"))
    assert "collect_contexts" in calls
    assert codec.pack_result(second) == codec.pack_result(first)


def test_an_oversized_report_is_not_remembered(store, tmp_path, monkeypatch):
    _forget(store)
    monkeypatch.setattr(fit_memo, "_MAX_BYTES", 8)
    _execute(_job(store, tmp_path / "one"))
    assert not list(Path(store.store.root).rglob("*.fit"))


def test_without_a_finished_day_nothing_is_remembered(tmp_path):
    calls = []
    views = {"a": _Unplaced()}
    assert fit_memo.remembered_room(views, {}, lambda: calls.append(1) or {"x": 1}) == {"x": 1}
    assert fit_memo.remembered_room(views, {}, lambda: calls.append(1) or {"x": 1}) == {"x": 1}
    assert len(calls) == 2


class _Unplaced:
    _days = None


def _edited(job, edit):
    """``job`` with its parameters changed by ``edit``."""
    parameters = codec.decode(job["parameters"])
    edit(parameters)
    return {**job, "parameters": codec.encode(parameters)}


def _member(parameters, rid):
    return parameters["members"][GROUP][rid]


def _room_labels(parameters):
    return parameters["rooms"][GROUP]["room_labels"][GROUP]


PARAMETER_EDITS = {
    "weight": lambda p: p.update(weight=0.25),
    "start": lambda p: p.update(start=p["start"] + 1),
    "end": lambda p: p.update(end=p["end"] - 1),
    "threshold of the first member": lambda p: _member(p, "radar_a")["thresholds"].update(
        g0_move=51
    ),
    "threshold of the second member": lambda p: _member(p, "radar_b")["thresholds"].update(
        g0_move=51
    ),
    "timing of the first member": lambda p: _member(p, "radar_a")["timing"].update(hold=1),
    "timing of the second member": lambda p: _member(p, "radar_b")["timing"].update(hold=1),
    "constraints of a member": lambda p: _member(p, "radar_b").update(constraints={"g0_move": {}}),
    "room label edge": lambda p: _room_labels(p)[0].update(end=_room_labels(p)[0]["end"] - 0.25),
    "room label state": lambda p: _room_labels(p)[0].update(state="unknown"),
    "room label source": lambda p: _room_labels(p)[0].update(source="manual"),
    "room label removed": lambda p: _room_labels(p).pop(),
    "room definition": lambda p: p["rooms"][GROUP].update(groups={"other": {}}),
}


def _member_label(rid):
    def edit(view):
        view.data["devices"][rid]["history_labels"].append(
            {"start": 5.0, "end": 6.0, "state": "present"}
        )

    return edit


def _snapshot_label(view):
    view.data["room_snapshot"]["room_labels"][GROUP][0]["end"] -= 0.5


def _member_sources(view):
    view.data["devices"]["radar_b"]["presence_sources"] = {"entity": "x"}


VIEW_EDITS = {
    "labels of the first member": _member_label("radar_a"),
    "labels of the second member": _member_label("radar_b"),
    "room labels a view holds": _snapshot_label,
    "member sources": _member_sources,
    "history cutoff": lambda view: setattr(view, "_history_until", view._history_until - 1),
    "attribution cutoff": lambda view: setattr(view, "_attribution_until", 1.0),
    "attribution window": lambda view: setattr(view, "_attribution_since", 1.0),
    "physical start": lambda view: setattr(view, "_physical_history_start", 1.0),
    "history start": lambda view: setattr(view, "_snapshot_history_start", 1.0),
}


@pytest.fixture(scope="module")
def base(store, tmp_path_factory):
    return _job(store, tmp_path_factory.mktemp("job") / "job")


def _key(job, edit=None):
    views, parameters = _inputs(job)
    if edit is not None:
        for view in views.values():
            edit(view)
    return fit_memo._job_key(views, parameters)


def test_the_same_job_captured_again_has_the_same_key(store, base, tmp_path):
    assert _key(base) == _key(_job(store, tmp_path / "again"))


@pytest.mark.parametrize("name", PARAMETER_EDITS)
def test_each_parameter_of_a_room_fit_changes_its_key(base, name):
    assert _key(_edited(base, PARAMETER_EDITS[name])) != _key(base)


@pytest.mark.parametrize("name", VIEW_EDITS)
def test_each_view_input_of_a_room_fit_changes_its_key(base, name):
    assert _key(base, VIEW_EDITS[name]) != _key(base)


def test_budgets_change_the_key(base):
    views, parameters = _inputs(base)
    before = fit_memo._job_key(views, parameters)
    views["radar_a"]._days.budgets = {**views["radar_a"]._days.budgets, "max_rows": 1}
    assert fit_memo._job_key(views, parameters) != before


def test_the_code_changes_the_key(base, monkeypatch):
    before = _key(base)
    monkeypatch.setattr(fit_memo, "_producer", lambda: "other code")
    assert _key(base) != before


def _copy_day_entries(view, rid, *, persistent):
    """The entry of ``rid``'s newest finished or open day."""
    days = view._days
    return next(
        (rid, date)
        for date in reversed(days.days(rid))
        if days.entries[rid, date]["persistent"] == persistent
    )


def test_a_rewritten_finished_day_changes_the_key(store, tmp_path, base):
    # Its own store: the file is touched, so a job captured before it would be refused.
    root = tmp_path / "store"
    own = build(root, days=2, open_seconds=600, sensor_days=1)
    job = request(own, tmp_path / "job", window_days=1, attribution_days=1)
    views, parameters = _inputs(job)
    before = fit_memo._job_key(views, parameters)
    key = _copy_day_entries(views["radar_a"], "radar_a", persistent=True)
    path = Path(views["radar_a"]._days.entries[key]["path"])
    found = path.stat()
    os.utime(path, ns=(found.st_atime_ns, found.st_mtime_ns + 5_000_000))
    again = request(own, tmp_path / "again", window_days=1, attribution_days=1)
    views, parameters = _inputs(again)
    assert fit_memo._job_key(views, parameters) != before


@pytest.mark.parametrize("rid", RADARS)
def test_changed_open_day_rows_change_the_key(store, tmp_path, rid):
    views, parameters = _inputs(_job(store, tmp_path / "job"))
    before = fit_memo._job_key(views, parameters)
    key = _copy_day_entries(views[rid], rid, persistent=False)
    path = Path(views[rid]._days.entries[key]["path"])
    raw = bytearray(path.read_bytes())
    raw[-1] ^= 1
    path.write_bytes(bytes(raw))
    entry = views[rid]._days.entries[key]
    assert entry["persistent"] is False
    assert fit_memo._job_key(views, parameters) != before


def test_a_late_guess_changes_the_key(store, base):
    views, parameters = _inputs(base)
    before = fit_memo._job_key(views, parameters)
    days = views["radar_a"]._days
    key = next(iter(days.entries))
    entry = dict(days.entries[key])
    entry["late"] = [[1.0, 2.0, 3.0, "present", 18, 0]]
    with patch.object(days, "entries", _Entries(days.entries, key, entry)):
        assert fit_memo._job_key(views, parameters) != before


class _Entries:
    """A manifest with one day's descriptor replaced."""

    def __init__(self, entries, key, entry):
        self.entries, self.key, self.entry = entries, key, entry

    def __iter__(self):
        return iter(self.entries)

    def get(self, key, default=None):
        return self.entry if key == self.key else self.entries.get(key, default)

    def __getitem__(self, key):
        return self.entry if key == self.key else self.entries[key]


# A fit's own key, over the problem it searches ----------------------------------------


@pytest.fixture(scope="module")
def problem(base):
    views, parameters = _inputs(base)
    members = {
        group: calculations._members(items, views) for group, items in parameters["members"].items()
    }
    rooms, start, end = parameters["rooms"], parameters["start"], parameters["end"]
    contexts = adaptive.collect_contexts(members, start, end, rooms)
    models = adaptive.models(contexts)
    return adapters.room_problem(members, start, end, rooms, 0.5, quality_contexts=models)


def _radar(problem, rid, **changes):
    radars = {**problem.radars, rid: replace(problem.radars[rid], **changes)}
    return replace(problem, radars=radars)


def _policy(problem, **changes):
    return replace(problem, policy=replace(problem.policy, **changes))


def _truth(problem, edit):
    zone = problem.zones[0]
    return replace(problem, zones=(replace(zone, truth=edit(zone.truth)),))


def _shifted(truth):
    first = truth[0]
    return (replace(first, end=first.end - 1), *truth[1:])


def _context(problem, rid):
    models = dict(problem.policy.quality["models"])
    context = replace(models[rid].context, fingerprint="other evidence")
    models[rid] = replace(models[rid], context=context)
    return _policy(problem, quality={**problem.policy.quality, "models": models})


FIT_EDITS = {
    "threshold": lambda p: _radar(p, "radar_a", current={0: 1}),
    "second member's threshold": lambda p: _radar(p, "radar_b", current={0: 1}),
    "timing": lambda p: _radar(p, "radar_a", timing={"hold": 1}),
    "second member's timing": lambda p: _radar(p, "radar_b", timing={"hold": 1}),
    "domain": lambda p: _radar(p, "radar_a", domains={0: (1, 2)}),
    "channels": lambda p: _radar(p, "radar_b", channels=(0,)),
    "weight": lambda p: _policy(p, automatic_weight=0.75),
    "policy": lambda p: _policy(p, reference=not p.policy.reference),
    "automatic horizon": lambda p: _policy(p, automatic_horizon_days=1),
    "human horizon": lambda p: _policy(p, human_evidence_hours=1),
    "support": lambda p: _policy(p, quality={**p.policy.quality, "zones": {"x": 1}}),
    "start": lambda p: replace(p, start=p.start + 1),
    "cut truth": lambda p: replace(p, end=p.zones[0].truth[0].end),
    "truth": lambda p: _truth(p, _shifted),
    "truth state": lambda p: _truth(
        p, lambda truth: (replace(truth[0], state="unknown"), *truth[1:])
    ),
    "truth source": lambda p: _truth(
        p, lambda truth: (replace(truth[0], source="human"), *truth[1:])
    ),
    "truth confidence": lambda p: _truth(
        p, lambda truth: (replace(truth[0], confidence=0.5), *truth[1:])
    ),
    "first member's evidence": lambda p: _context(p, "radar_a"),
    "second member's evidence": lambda p: _context(p, "radar_b"),
    "zones": lambda p: replace(p, zones=(replace(p.zones[0], members=("radar_a",)),)),
}


@pytest.mark.parametrize("name", FIT_EDITS)
def test_each_input_of_a_room_search_changes_its_key(problem, name):
    assert fit_memo._key(FIT_EDITS[name](problem)) != fit_memo._key(problem)


def test_the_same_room_problem_has_the_same_key(problem):
    assert fit_memo._key(problem) == fit_memo._key(replace(problem))


def test_a_later_end_with_nothing_more_to_read_is_the_same_search(problem):
    assert fit_memo._key(replace(problem, end=problem.end + 60)) == fit_memo._key(problem)


def test_rows_before_the_window_are_part_of_a_search(problem):
    class Earlier:
        def __init__(self, rows, extra):
            self.rows, self.extra = rows, extra

        def blocks(self, lo, hi):
            yield from self.extra
            yield from self.rows.blocks(lo, hi)

    block = next(problem.rows["radar_a"].blocks(problem.start, problem.end))
    rows = {**problem.rows, "radar_a": Earlier(problem.rows["radar_a"], [block])}
    assert fit_memo._key(replace(problem, rows=rows)) != fit_memo._key(problem)


def test_a_room_search_is_remembered_beside_a_finished_day(problem, store, base, monkeypatch):
    _forget(store)
    views, parameters = _inputs(base)
    members = {
        group: calculations._members(items, views) for group, items in parameters["members"].items()
    }
    searched = []
    plain = engine.fit

    def fit(found, **options):
        searched.append(found)
        return plain(found, **options)

    monkeypatch.setattr(engine, "fit", fit)
    fitter = fit_memo.room_fitter(members)
    first = fitter(problem)
    second = fitter(problem)
    assert len(searched) == 1
    assert second.thresholds == first.thresholds
    for name in ("quiet_cost", "selection_cost", "anomaly"):
        assert second.diagnostics.get(name) == first.diagnostics.get(name)
    assert fit_memo.room_fitter({"g": {"a": {"view": _Unplaced()}}})(problem) is not None
    assert len(searched) == 2
