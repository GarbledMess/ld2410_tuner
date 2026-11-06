"""A whole room Learn over a real-shaped store: every byte as the old attribution and counts gave."""

import json

import pytest
import test_tuner  # noqa: F401
from room_speed_fixture import build, request
from test_room_learn_attribution_columns import legacy_measure
from test_room_learn_row_counts import legacy_rows_in
from tuner_under_test.calibration import event_quality
from tuner_under_test.engine.hybrid import engine, exact, held
from tuner_under_test.history import day_results, fit_days, result_cache, spans, worker_snapshot
from tuner_under_test.history.fit_rows import CapturedRowSource
from tuner_under_test.rooms import adaptive, attribution_days
from tuner_under_test.worker import calculations, codec


def _forget_fits(root):
    for path in root.rglob("*.fit"):
        path.unlink()


def _cold(root):
    """As the first Learn over this store: no derived result in memory or on disk."""
    _forget_fits(root)
    for pattern in ("*.cell", "*.res"):
        for path in root.rglob(pattern):
            path.unlink()
    day_results.clear()
    result_cache._files.clear()


def _learn(job):
    return codec.pack_result(calculations.execute(job))


def _walked_context(view, key, keys, *, start, end, human_reader):
    """A room context as the row walk gave it."""
    return event_quality.collect(view, key, keys, start, end, human_reader, None)


def _separate_training_fits(training, weight, fit=None):
    """Each evidence policy compiled and replayed from nothing, as it was."""
    fit = engine.fit if fit is None else fit
    searched, found = {}, {}
    for policy in held.POLICIES:
        flag = held.reference_flag(policy, weight)
        if flag not in searched:
            searched[flag] = fit(held.with_reference(training, flag)).thresholds
        found[policy] = searched[flag]
    return found


def _legacy(monkeypatch):
    monkeypatch.setattr(attribution_days, "_measure", legacy_measure)
    monkeypatch.setattr(CapturedRowSource, "rows_in", legacy_rows_in)
    monkeypatch.setattr(adaptive, "collect", _walked_context)
    monkeypatch.setattr(exact.Evaluator, "alike", lambda _self, _rid, key: key)
    monkeypatch.setattr(held, "training_fits", _separate_training_fits)


def _without_fingerprints(value):
    """The one report value allowed to differ: no code reads an evidence fingerprint."""
    if isinstance(value, dict):
        return {k: _without_fingerprints(v) for k, v in value.items() if k != "fingerprint"}
    if isinstance(value, (list, tuple)):
        return [_without_fingerprints(item) for item in value]
    return value


def _comparable(raw):
    return json.dumps(_without_fingerprints(codec.unpack_result(raw)), sort_keys=True)


def _fingerprints(value):
    """Every evidence fingerprint in a report, for the proof that only they changed."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "fingerprint":
                yield item
            else:
                yield from _fingerprints(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _fingerprints(item)


def _scenario(tmp_path, monkeypatch, *, days, window_days, attribution_days_kept, sensor_days):
    monkeypatch.setattr(worker_snapshot, "_executor_only", lambda: None)
    fixture = build(tmp_path, days=days, sensor_days=sensor_days)
    return request(
        fixture, tmp_path / "job", window_days=window_days, attribution_days=attribution_days_kept
    )


def _assert_identical(tmp_path, monkeypatch, job):
    _cold(tmp_path)
    cold = _learn(job)
    remembered = _learn(job)
    _forget_fits(tmp_path)
    warm = _learn(job)
    _cold(tmp_path)
    with monkeypatch.context() as patch:
        _legacy(patch)
        old_cold = _learn(job)
        _forget_fits(tmp_path)
        old_warm = _learn(job)
    assert cold == remembered == warm
    assert old_cold == old_warm
    assert _comparable(cold) == _comparable(old_cold)
    assert set(_fingerprints(codec.unpack_result(cold)))
    return codec.unpack_result(cold)


def test_room_learn_bytes_match_the_old_path(tmp_path, monkeypatch):
    job = _scenario(
        tmp_path, monkeypatch, days=5, window_days=4, attribution_days_kept=3, sensor_days=4
    )
    result = _assert_identical(tmp_path, monkeypatch, job)
    assert set(result["thresholds"]) == {"radar_a", "radar_b"}
    assert result["thresholds"]["radar_a"]


def test_room_learn_reads_each_day_for_counting_once(tmp_path, monkeypatch):
    """A job's budgets read a day's rows only for a window not yet counted, however many plans:
    the only repeats left are the held-out plans' own cuts of a day, at most one a plan."""
    job = _scenario(
        tmp_path, monkeypatch, days=4, window_days=3, attribution_days_kept=2, sensor_days=3
    )
    _cold(tmp_path)
    plain, count = fit_days.FittingDays.physical, CapturedRowSource.rows_in
    reads, counting = [], [False]

    def counted(self, device_id, date, channels):
        if counting[0]:
            reads.append((device_id, date))
        return plain(self, device_id, date, channels)

    def budget(self, lo, hi):
        counting[0] = True
        try:
            return count(self, lo, hi)
        finally:
            counting[0] = False

    monkeypatch.setattr(fit_days.FittingDays, "physical", counted)
    monkeypatch.setattr(CapturedRowSource, "rows_in", budget)
    _learn(job)
    assert reads
    assert len(reads) <= len(set(reads)) * 2


def test_room_learn_walks_no_span_for_attribution(tmp_path, monkeypatch):
    job = _scenario(
        tmp_path, monkeypatch, days=4, window_days=3, attribution_days_kept=2, sensor_days=3
    )
    _cold(tmp_path)
    walked = []
    plain = attribution_days._measure

    def measured(*args):
        made = len(walked)
        original = spans.Span

        def counted(*fields):
            walked.append(fields)
            return original(*fields)

        with monkeypatch.context() as patch:
            patch.setattr(spans, "Span", counted)
            result = plain(*args)
        assert len(walked) == made
        return result

    monkeypatch.setattr(attribution_days, "_measure", measured)
    _learn(job)


@pytest.mark.scaling
def test_full_shape_room_learn_bytes_match_the_old_path(tmp_path, monkeypatch):
    job = _scenario(
        tmp_path, monkeypatch, days=60, window_days=28, attribution_days_kept=7, sensor_days=10
    )
    _assert_identical(tmp_path, monkeypatch, job)
