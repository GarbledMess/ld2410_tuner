"""Sensor room labels are machine-written many times a day, so they must age out."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_tuner  # noqa: F401
from tuner_under_test.history import policy, recording
from tuner_under_test.rooms import room_labels

NOW = 2_000_000_000.0
DAY = 86400.0


def label(age_days, source="sensor", length=60.0):
    end = NOW - age_days * DAY
    return {"start": end - length, "end": end, "state": "present", "source": source}


def owner(labels, settings=None):
    data = {"devices": {}, "room_labels": {"area:a": labels}}
    if settings:
        data["storage_settings"] = {**policy.DEFAULTS, **settings}
    return SimpleNamespace(data=data)


def test_old_sensor_labels_expire_and_recent_ones_stay():
    recent = label(29)
    runtime = owner([label(31), recent])
    assert room_labels.expire_sensor(runtime, NOW)
    assert runtime.data["room_labels"]["area:a"] == [recent]
    assert runtime.data["room_sensor_revision"]["area:a"] == 1


def test_manual_room_labels_never_expire_here():
    manual = label(400, "manual")
    runtime = owner([manual, label(31)])
    room_labels.expire_sensor(runtime, NOW)
    assert runtime.data["room_labels"]["area:a"] == [manual]


def test_a_label_crossing_the_cutoff_stays_whole():
    crossing = label(29.5, length=2 * DAY)
    runtime = owner([crossing])
    assert not room_labels.expire_sensor(runtime, NOW)
    assert runtime.data["room_labels"]["area:a"] == [crossing]
    assert "room_sensor_revision" not in runtime.data


def test_the_longer_retention_setting_decides():
    kept = label(50)
    runtime = owner([kept], {"human_days": 60, "automatic_days": 30})
    assert not room_labels.expire_sensor(runtime, NOW)
    assert runtime.data["room_labels"]["area:a"] == [kept]


def test_no_room_labels_is_fine():
    runtime = SimpleNamespace(data={"devices": {}})
    assert not room_labels.expire_sensor(runtime, NOW)


def test_retention_expires_labels_even_while_cleanup_backs_off():
    runtime = owner([label(31)])
    runtime.data["maintenance_retry"] = {"after": NOW + DAY, "delay": DAY}
    runtime._schedule_save = Mock()
    with patch("time.time", return_value=NOW):
        asyncio.run(recording._retain(runtime))
    assert runtime.data["room_labels"]["area:a"] == []
    runtime._schedule_save.assert_called_once()
