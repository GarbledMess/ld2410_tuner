"""Startup recovery operates on staged files and hands off raw bounded columns."""

import asyncio
import copy
import datetime
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import test_tuner  # noqa: F401
from legacy_formats import samples
from tuner_under_test.history import dayfile, daystore

START = 1_760_486_400.0
NOW = START + 100


def test_recovered_live_handoff_does_not_replay_or_decompress(tmp_path):
    from tuner_under_test.worker import startup

    root = tmp_path / "history"
    source = daystore.DayStore(root)
    source.append("radar", dayfile.Change(START, {0: 12.5}))
    source.append("radar", dayfile.Guess(START + 2, "present", 20, 1))
    source.append("radar", dayfile.Change(START + 4, {0: 13}))
    source.flush(NOW)
    result = startup.execute({"root": str(root), "data": {"devices": {}}, "now": NOW})
    with (
        patch.object(dayfile, "decode_log", side_effect=AssertionError("parent replay")),
        patch.object(dayfile, "decode_day", side_effect=AssertionError("parent inflate")),
    ):
        recovered = startup.load(result, root)
    assert recovered._live["radar"].builder.current is None
    assert recovered._live["radar"].builder.columns[0][0] == 12.5
    assert recovered._live["radar"].builder.guesses == [(START + 2, START + 4, "present", 20, 1)]
    recovered.append("radar", dayfile.Change(NOW, {0: 25}))
    recovered.flush(NOW)
    assert recovered.day("radar", datetime.date(2025, 10, 15)).vector(2)[0] == 25


def test_legacy_conversion_returns_mutations_without_modifying_source_metadata(tmp_path):
    from tuner_under_test.worker import startup

    data = {
        "devices": {
            "radar": {
                "history": [samples(START, [10, 20])],
                "samples": [],
                "last_learning": {"old": True},
                "label_revision": 7,
            }
        }
    }
    original = copy.deepcopy(data)
    result = startup.execute({"root": str(tmp_path), "data": data, "now": NOW})
    assert data == original
    assert result["patches"]["radar"] == {
        "remove": ["history", "samples", "last_learning"],
        "set": {
            "history_cleanup": {"invalid_blocks": 0, "untimed_summaries": 1},
            "label_revision": 8,
        },
    }
    recovered = startup.load(result, tmp_path)
    assert list(recovered._live["radar"].builder.columns[0]) == [10, 20]


def test_handoff_refuses_oversized_counts_before_opening_file(tmp_path):
    from tuner_under_test.worker import startup

    with pytest.raises(ValueError, match="budget"):
        startup.load(
            {"live": [{"device": "a", "rows": startup.MAX_ROWS + 1}], "patches": {}}, tmp_path
        )


def test_publish_preserves_store_identity_and_legacy_patch(tmp_path):
    from tuner_under_test.worker import startup

    root, job = tmp_path / "history", tmp_path / "job"
    root.mkdir()
    (root / "old").write_bytes(b"original")
    data = {"devices": {"radar": {"history": [samples(START, [10, 20])]}}}
    owner = daystore.DayStore(root)
    runtime = SimpleNamespace(_days=owner, data=data)
    result = startup.execute(
        {"root": str(job / "history"), "directory": str(job), "data": data, "now": NOW}
    )
    startup.publish(runtime, owner, result, job)
    assert runtime._days is owner
    assert owner._live["radar"].builder.columns[0] == bytearray([10, 20])
    assert "history" not in data["devices"]["radar"]
    assert not (root / "old").exists()


def test_interrupted_publication_restores_stable_original_backup(tmp_path):
    from tuner_under_test.worker import startup

    root = tmp_path / "history"
    backup = root.with_name(root.name + ".startup-backup")
    backup.mkdir()
    (backup / "original").write_bytes(b"safe")
    startup.recover_publication(root)
    assert (root / "original").read_bytes() == b"safe"
    assert not backup.exists()


def test_raw_day_round_trip_preserves_fractional_values_and_guesses(tmp_path):
    from tuner_under_test.worker import startup

    builder = dayfile.DayBuilder(START, START + 86400)
    builder.add(dayfile.Change(START, {0: 2.25}))
    builder.add(dayfile.Guess(START, "present", 20, 1))
    builder.add(dayfile.End(START + 10))
    original = builder.day()
    descriptor = startup.write_day(original, tmp_path, 0)
    restored = startup.read_day(tmp_path, descriptor)
    assert restored.vector(0)[0] == 2.25
    assert restored.guesses == original.guesses
    assert list(restored.ends) == list(original.ends)


@pytest.mark.parametrize(
    "change",
    [{"rows": -1}, {"rows": True}, {"guesses": 2**63}, {"floats": []}, {"floats": [0] * 18}],
)
def test_invalid_handoff_counts_and_flags_are_rejected(tmp_path, change):
    from tuner_under_test.worker import startup

    descriptor = {"rows": 0, "floats": [False] * 18, **change}
    with pytest.raises(ValueError):
        startup.load({"live": [descriptor]}, tmp_path)


def test_decoded_handoff_budget_rejects_before_file_reads(tmp_path):
    from unittest.mock import patch

    from tuner_under_test.worker import startup

    # Device and row counts alone no longer reject; restored memory does.
    # Each 2M-row day fits (~77 MB decoded); three exceed the 192 MiB total.
    live = [{"device": str(i), "rows": 2_000_000, "floats": [False] * 18} for i in range(3)]
    startup._budget({"live": live[:2]})
    with patch.object(startup, "_read", side_effect=AssertionError("read before budget")):
        with pytest.raises(ValueError, match="decoded heap budget"):
            startup.load({"live": live}, tmp_path)


@pytest.mark.parametrize("name", ["../escape", "other.bin", 3])
def test_live_path_is_confined(tmp_path, name):
    from tuner_under_test.worker import startup

    with pytest.raises(ValueError, match="path"):
        startup.load_live(tmp_path, {"file": name})


def test_truncated_handoff_and_duplicate_devices_are_rejected(tmp_path):
    from tuner_under_test.worker import startup

    live = daystore._Live(datetime.date(2025, 10, 15))
    descriptor = startup.freeze_live("a", live, tmp_path, 0)
    with pytest.raises(ValueError, match="Duplicate"):
        startup.load({"live": [descriptor, descriptor]}, tmp_path)
    (tmp_path / descriptor["file"]).write_bytes(b"broken")
    with pytest.raises(ValueError, match="size"):
        startup.load_live(tmp_path, descriptor)


def test_confirmed_spans_and_open_state_survive_raw_handoff(tmp_path):
    from tuner_under_test.worker import protocol, startup

    live = daystore._Live(datetime.date(2025, 10, 15))
    live.add(dayfile.Change(START, {0: 3}))
    live.add(dayfile.GuessSpan(START + 20, START, START + 10, "present", 20, 1))
    live.add(dayfile.Guess(START + 20, "present", 19, 1))
    descriptor = startup.freeze_live("a", live, tmp_path, 0)
    descriptor = protocol.unpack(protocol.pack("result", descriptor), "result")
    restored = startup.load_live(tmp_path, descriptor)
    assert restored.builder.current == live.builder.current
    assert restored.builder.run == live.builder.run
    assert restored.builder.confirmed == live.builder.confirmed


def _open_handoff(directory):
    from tuner_under_test.worker import protocol, startup

    live = daystore._Live(datetime.date(2025, 10, 15))
    live.add(dayfile.Change(START, {0: 3}))
    live.add(dayfile.Guess(START + 20, "present", 19, 1))
    descriptor = startup.freeze_live("a", live, directory, 0)
    return protocol.unpack(protocol.pack("result", descriptor), "result")


@pytest.mark.parametrize(
    "field,value",
    [
        ("current", [3] * 17),
        ("current", ["3"] + [None] * 17),
        ("current", [float("nan")] + [None] * 17),
        ("run", [START + 20, "maybe", 19, 1]),
        ("run", [START + 20, "present", 19]),
        ("run", [START + 20, "present", 256, 1]),
        ("version", "1"),
        ("last", None),
    ],
)
def test_inconsistent_live_state_is_rejected(tmp_path, field, value):
    """Regression: an impossible open builder was installed and broke the next append."""
    from tuner_under_test.worker import startup

    descriptor = {**_open_handoff(tmp_path), field: value}
    with pytest.raises(ValueError, match="live state"):
        startup.load({"live": [descriptor]}, tmp_path)


def test_an_open_vector_without_a_row_is_rejected(tmp_path):
    from tuner_under_test.worker import startup

    descriptor = startup.freeze_live("a", daystore._Live(datetime.date(2025, 10, 15)), tmp_path, 0)
    descriptor["current"] = [20] * 18
    with pytest.raises(ValueError, match="live state"):
        startup.load({"live": [descriptor]}, tmp_path)


def test_consistent_open_live_state_is_accepted(tmp_path):
    from tuner_under_test.worker import startup

    recovered = startup.load({"live": [_open_handoff(tmp_path)]}, tmp_path)
    recovered.append("a", dayfile.Change(START + 30, {0: 4}))
    assert list(recovered._live["a"].builder.columns[0]) == [3, 4]


@pytest.mark.parametrize("obstruction", ["owner", "backup", "missing-stage"])
def test_publication_failure_keeps_original_history(tmp_path, obstruction):
    from tuner_under_test.worker import startup

    root, job = tmp_path / "history", tmp_path / "job"
    root.mkdir()
    job.mkdir()
    (root / "original").write_bytes(b"safe")
    owner = daystore.DayStore(root)
    runtime = SimpleNamespace(_days=owner, data={"devices": {}})
    if obstruction == "owner":
        runtime._days = None
    elif obstruction == "backup":
        root.with_name(root.name + ".startup-backup").mkdir()
    with pytest.raises((ValueError, FileNotFoundError)):
        startup.publish(runtime, owner, {"live": [], "patches": {}}, job)
    assert (root / "original").read_bytes() == b"safe"


def test_completed_publication_discards_old_backup(tmp_path):
    from tuner_under_test.worker import startup

    root = tmp_path / "history"
    root.mkdir()
    backup = root.with_name(root.name + ".startup-backup")
    backup.mkdir()
    startup.recover_publication(root)
    startup.recover_publication(root)
    assert root.exists()
    assert not backup.exists()


def test_initialize_uses_owned_transaction_and_accepts_only_reaped_result(tmp_path):
    from tuner_under_test.worker import startup, transactions

    root, job = tmp_path / "history", tmp_path / "job"
    data = {"devices": {"radar": {"history": [samples(START, [10])]}}}
    owner = daystore.DayStore(root)

    async def executor(function, *args):
        return await asyncio.to_thread(function, *args)

    runtime = SimpleNamespace(
        _days=owner, data=data, hass=SimpleNamespace(async_add_executor_job=executor)
    )

    async def transport(runtime_arg, operation, input_data, accept):
        assert runtime_arg is runtime
        assert operation == "startup"
        result = startup.execute(
            {"root": str(job / "history"), "directory": str(job), "data": input_data, "now": NOW}
        )
        await accept(result, job)

    with patch.object(transactions, "run", side_effect=transport):
        asyncio.run(startup.initialize(runtime))
    assert runtime._days is owner
    assert list(owner._live["radar"].builder.columns[0]) == [10]


def test_handoff_changed_after_stat_is_rejected(tmp_path):
    from tuner_under_test.worker import startup

    descriptor = startup.freeze_live("a", daystore._Live(datetime.date(2025, 10, 15)), tmp_path, 0)
    target = tmp_path / descriptor["file"]
    found = target.stat()
    target.write_bytes(b"changed")
    with patch.object(startup.os, "fstat", return_value=found):
        with pytest.raises(ValueError, match="Changed"):
            startup.load_live(tmp_path, descriptor)


def test_unconverted_metadata_has_no_patch(tmp_path):
    from tuner_under_test.worker import startup

    result = startup.execute(
        {"root": str(tmp_path), "data": {"devices": {"a": {"reference_profile": {}}}}, "now": NOW}
    )
    assert result["patches"] == {}


def test_startup_migration_requires_only_standard_library(tmp_path):
    from tuner_under_test.worker import startup

    package = str(Path(startup.__file__).parents[1])
    request = {
        "root": str(tmp_path),
        "data": {"devices": {"a": {"history": [samples(START, [12, 20])]}}},
        "now": NOW,
    }
    script = "\n".join(
        [
            "import sys, types, json",
            f"p = types.ModuleType('pure_tuner'); p.__path__ = [{json.dumps(package)}]",
            "sys.modules[p.__name__] = p",
            "from pure_tuner.worker import startup",
            "result = startup.execute(json.loads(sys.argv[1]))",
            "assert result['patches']['a']['remove'] == ['history']",
            "assert not any(name.startswith('homeassistant') for name in sys.modules)",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", script, json.dumps(request)],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()


def test_initialize_publishes_legacy_summary_cleanup_on_executor(tmp_path):
    from tuner_under_test.worker import startup, supervisor
    from worker_transport import Transport

    root = tmp_path / "history"
    folder = root / "radar"
    folder.mkdir(parents=True)
    legacy = folder / "1970-01-01.sum"
    legacy.write_bytes(b"legacy")
    temporary = folder / "interrupted.sumtmp"
    temporary.write_bytes(b"partial")
    result = folder / "1970-01-01.res"
    result.write_bytes(b"keep result")
    owner = daystore.DayStore(root)
    runtime = SimpleNamespace(
        _days=owner,
        data={"devices": {}},
        hass=SimpleNamespace(async_add_executor_job=asyncio.to_thread),
    )
    transport = Transport()

    async def process(service, args):
        return await transport.process(args)

    with patch.object(supervisor.Supervisor, "_process", process):
        asyncio.run(startup.initialize(runtime))
    assert not legacy.exists()
    assert not temporary.exists()
    assert result.read_bytes() == b"keep result"
    assert runtime._days is owner
