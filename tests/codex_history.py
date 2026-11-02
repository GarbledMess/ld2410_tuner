"""Synthetic native history fixtures for the parallel history test ports."""

from types import SimpleNamespace

import test_tuner as harness
from tuner_under_test.history import dayfile, guesses, ingest


def view_with_runs(labels=(), runs=(), rows=None):
    view = SimpleNamespace(
        data={"devices": {"a": {"history_labels": list(labels)}}, "room_snapshot": None}, hass=None
    )
    extra = []
    starts = {run[0] for run in runs}
    for start, end, state, level, basis in runs:
        extra.append(dayfile.Guess(start, state, level, basis))
        if end not in starts:
            extra.append(dayfile.Guess(end, None, 0, 0))
    if rows is None:
        rows = [(float(i), bytes([10] * 18)) for i in range(10)]
    times = sorted({t for t, _ in rows})
    for before, after in zip(times, times[1:], strict=False):
        if after - before > guesses.GAP:
            for low, high, state, level, basis in runs:
                if low < after < high:
                    extra.append(dayfile.Guess(after, state, level, basis))
    harness.record_rows(view, "a", rows, extra)
    return view


def sample_guesses(samples):
    """Deliver guesses through native ingestion; close each synthetic recording at +3 s."""
    view = SimpleNamespace(data={"devices": {"a": {}}}, _live={}, _storage_status={})
    harness._store(view)
    previous = None
    for timestamp, found in samples:
        if previous is not None and timestamp - previous > guesses.GAP:
            ingest.end(view, "a", previous + 3)
        ingest.change(view, "a", {"g0_move": 30}, timestamp)
        ingest.guess(view, "a", timestamp, found)
        previous = timestamp
    if previous is not None:
        ingest.end(view, "a", previous + 3)
        view._history_until = previous + 3
    return guesses.of(view, "a").runs


class ObservedEvidence:
    """Test-side scalar view of streamed TruthSpans, never a production objective."""

    def __init__(self, problem):
        self.problem = problem

    @property
    def regions(self):
        from tuner_under_test.engine.hybrid.diagnostics import _pieces

        result = []
        zone = self.problem.zones[0]
        for a, b, span, values in _pieces(self.problem, "a", zone.truth):
            row = (a, b, {"g0_move": values[0]}, span.state, span.source, span.confidence)
            if result and result[-1][1] == a and result[-1][2:] == row[2:]:
                result[-1] = (result[-1][0], b, *row[2:])
            else:
                result.append(row)
        return result

    @property
    def shares(self):
        from tuner_under_test.calibration.scoring import episode_share

        support = self.problem.policy.quality
        return tuple(
            episode_share(
                self.problem.policy.automatic_weight,
                support["human_episodes"][state],
                support["automatic_episode_mass"][state],
            )
            for state in ("present", "not_present")
        )


def objective(view, *, start=0, end=None, weight=1.0):
    """Expose observed hybrid truth as scalar test tuples with recorded confidence."""
    from tuner_under_test.engine.hybrid.adapters import device_problem

    return ObservedEvidence(
        device_problem(
            view,
            "a",
            ["g0_move"],
            {"g0_move": 20},
            {},
            weight,
            {},
            start,
            view._history_until if end is None else end,
            quality_context=False,
        )
    )


def diagnostic_samples(evidence, human):
    from dataclasses import replace

    from tuner_under_test.engine.hybrid.diagnostics import _radar

    problem = evidence.problem
    zone = problem.zones[0]
    zone = replace(zone, truth=tuple(s for s in zone.truth if (s.source == "human") == human))
    problem = replace(problem, zones=(zone,))
    return _radar(problem, "a", {"a": {0: 20}}, zone.truth)["samples"]
