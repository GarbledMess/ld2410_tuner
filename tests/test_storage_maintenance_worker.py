"""Storage planning runs on a private tree and returns raw bounded handoffs."""

from pathlib import Path

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.history import budget, dayfile, daystore, policy


def test_private_maintenance_matches_original_plan_without_publishing(tmp_path):
    from tuner_under_test.worker import maintenance, transactions

    original = daystore.DayStore(tmp_path / "original")
    now = 2_000_000_000
    timestamp = now - 10 * policy.DAY
    date = daystore.date_of(timestamp)
    builder = dayfile.DayBuilder(*daystore.bounds(date))
    builder.add(dayfile.Change(timestamp, {0: 25}))
    builder.add(dayfile.End(timestamp + 6))
    original.replace("a", date, builder.day())
    data = {"devices": {"a": {"history_labels": []}}}
    expected, changes, report = budget.prepare_days(original, data, now, clean=True)
    directory = tmp_path / "job"
    transactions.copy_tree(original.root, directory / "history")
    before = original._path("a", date, ".day").read_bytes()
    result = maintenance.execute(
        {
            "root": str(directory / "history"),
            "directory": str(directory),
            "data": data,
            "now": now,
            "clean": True,
            "target": None,
            "live": {},
        }
    )
    prepared, actual, actual_report, staged = maintenance.load(result, directory)
    assert prepared == expected
    assert actual_report == report
    assert len(actual) == len(changes) == len(staged) == 1
    assert actual[0][:2] == changes[0][:2]
    assert actual[0][2] == changes[0][2]
    assert Path(staged[0][3]).exists()
    assert original._path("a", date, ".day").read_bytes() == before


def test_handoff_rejects_path_outside_private_job(tmp_path):
    from tuner_under_test.worker import maintenance

    with pytest.raises(ValueError, match="path"):
        maintenance.load(
            {
                "prepared": {},
                "report": {},
                "changes": [
                    {"key": "a", "date": "1970-01-01", "temporary": "../outside", "day": {}}
                ],
            },
            tmp_path,
        )


@pytest.mark.parametrize("advance", [0, 19])
def test_live_handoff_reopens_run_without_duplicating_recovery_close(tmp_path, advance):
    from tuner_under_test.worker import maintenance, transactions

    now = 2_000_000_000
    start = now - 100
    original = daystore.DayStore(tmp_path / "original")
    original.append("a", dayfile.Change(start, {0: 25}))
    original.append("a", dayfile.Guess(start, "present", 16, 0))
    if advance:
        original.append("a", dayfile.Change(start + advance, {0: 30}))
    original.flush(now)
    live = original._live["a"]
    expected = original.day("a", live.date, until=now)
    directory = tmp_path / "job"
    transactions.copy_tree(original.root, directory / "history")
    recovered = daystore.DayStore(directory / "history")
    recovered.open(now)
    maintenance._restore_live(
        recovered,
        {
            "a": {
                "date": str(live.date),
                "last": live.last,
                "seen": live.seen,
                "current": list(live.builder.current),
                "run": list(live.builder.run),
            }
        },
    )
    actual = recovered.day("a", live.date, until=now)
    assert actual == expected
    assert actual.guesses == [(start, now, "present", 16, 0)]
    device = {"history_labels": [{"start": start, "end": now, "state": "present"}]}
    assert list(budget._held_ranges(actual, device)) == list(budget._held_ranges(expected, device))
    assert budget._day_histograms(recovered, "a", device, {}, now, 0) == budget._day_histograms(
        original, "a", device, {}, now, 0
    )
